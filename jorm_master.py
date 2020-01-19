#!/usr/bin/env python3

# Commander of multiple Jormungander runners for ensuring high availability
#
# This script handles running multiple Jormungandr instances in a way that no adversarial forks should be possible.
# At the same time reports current height to PoolTool website and restarts the runners if they get stuck.
#
# author: Tomas Stefan <ts@stdin.cz>, NEO pool

import requests
import logging
import yaml
from time import sleep, time
from systemd.journal import JournaldLogHandler
from enum import IntEnum
from subprocess import run
from datetime import datetime


# load the configuration from a file
with open('/etc/cardano/jorm_master.yaml', 'r') as f:
    config = yaml.safe_load(f)

p = config['pooltool']
pooltool_endp = f'{p["endp_tip"]}?poolid={p["pool_id"]}&userid={p["user_id"]}&genesispref={p["genesis"][:14]}&mytip='
del(p)


# Initialize systemd logging
logger = logging.getLogger('jorm_master')
journald_handler = JournaldLogHandler()
journald_handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
logger.addHandler(journald_handler)
logger.setLevel(logging.INFO)


def unix_time(time_str):
    """Converts time in format '2019-12-13T19:13:37+00:00' to unix time
    """
    t = time_str[:22] + time_str[23:]  # get rid of the ':' in time zone
    return int(datetime.strptime(t, '%Y-%m-%dT%H:%M:%S%z').timestamp())


class Status(IntEnum):
    OFF = 1
    BOOT = 2
    ON = 3


class Runner:
    def __init__(self, id):
        self.booting = False
        self.boot_start_time = time()  # don't kill booting runners after jorm_manager restart
        self.boot_end_time = 0
        self.__id = id
        self.__session = requests.Session()
        self.__rest = f'http://127.0.0.1:{config["rest_prefix"]}{id}'

    def __node_stats(self):
        """Return node stats in JSON format for a specific instance. Passes exceptions
        """
        return self.__session.get(f'{self.__rest}/api/v0/node/stats').json()

    def __settings(self, key):
        """Return settings value for specified key, or None if unavailable
        """
        try:
            logger.info(f'Reading property {key} from settings')
            res = self.__session.get(f'{self.__rest}/api/v0/settings').json()[key]
            logger.info(f'Obtained {key}: {res}')
            return res
        except:
            return None

    def status(self):
        """Return runner's status - ON/BOOT/OFF
        """
        try:
            return Status.ON if self.__node_stats()['state'] == 'Running' else Status.BOOT
        except:
            # Make sure the service is really stopped (REST API issues)
            if run(['systemctl', 'is-active', '--quiet', f'jorm_runner@{self.__id}']).returncode == 0:
                logger.error(f'Jormungandr runner {self.__id} not responding to REST api requests, stopping')
                self.stop()
            return Status.OFF

    def restart(self):
        """(Re)start Jormungandr runner
        """
        logger.info(f'(Re)starting Jormungandr runner {self.__id}')
        run(['systemctl', 'restart', f'jorm_runner@{self.__id}.service'])
        self.boot_start_time = time()
        self.booting = True

    def stop(self):
        """Stop Jormungandr runner
        """
        logger.info(f'Stopping Jormungandr runner {self.__id}')
        run(['systemctl', 'stop', f'jorm_runner@{self.__id}.service'])
        self.booting = False

    def height(self):
        """Return current block height, or 0 if unavailable
        """
        try:
            return int(self.__node_stats()['lastBlockHeight'])
        except:
            return 0

    def block_0_time(self):
        """Return block 0 time from settings, or None if unavailable
        """
        time_text = self.__settings('block0Time')  # e.g. '2019-12-13T19:13:37+00:00'
        return unix_time(time_text) if time_text else None

    def slot_duration(self):
        """Return slot duration from settings, or None if unavailable
        """
        slot_duration = self.__settings('slotDuration')
        return int(slot_duration) if slot_duration else None

    def slots_per_epoch(self):
        """Return number of slots per epoch, or None if unavailable
        """
        slots_per_epoch = self.__settings('slotsPerEpoch')
        return int(slots_per_epoch) if slots_per_epoch else None

    def leader_ids(self):
        """Return list of all leader IDs
        """
        try:
            return self.__session.get(f'{self.__rest}/api/v0/leaders').json()
        except:
            return []

    def is_leader(self):
        """Check if runner is in leader mode
        """
        return bool(self.leader_ids())  # False on empty list, otherwise True

    def leader_events(self):
        """Return list of all leader event times for the current epoch
        """
        res = []
        curr_time = time()

        try:
            events = self.__session.get(f'{self.__rest}/api/v0/leaders/logs').json()
            for e in events:
                t = e['scheduled_at_time']
                event_time = unix_time(t)
                if event_time > curr_time:
                    logger.info(f'Found leader event at {t}')
                    res += [event_time]
            logger.info(f'There are {len(res)} leader events remaining in the current epoch')
        except:
            pass

        return res

    def promote(self):
        """Make passive node a leader
        """
        try:
            logger.info(f'Promoting Jormungandr runner {self.__id} to leader')
            with open(config['node_secret'], 'r') as f:
                secret = yaml.safe_load(f)
                self.__session.post(f'{self.__rest}/api/v0/leaders', json=secret).raise_for_status()
        except:
            logger.error(f'Cannot promote Jormungandr runner {self.__id} to leader')

    def demote(self):
        """Make the runner a passive node without the possibility to create blocks
        """
        try:
            for leader_id in self.leader_ids():
                logger.info(f'Removing leader id {leader_id} from Jormungandr runner {self.__id}')
                self.__session.delete(f'{self.__rest}/api/v0/leaders/{leader_id}').raise_for_status()
        except:
            logger.error(f'Cannot demote Jormungandr runner {self.__id}')


class PoolTool:
    def __init__(self):
        self.__session = requests.Session()
        self.__majority_max = 0
        self.__last_sent = 0
        self.__last_recv = 0
        self.__last_height = 1

    def send_height(self, height):
        """Send the current height tip to the PoolTool website
        """
        if time() - self.__last_sent < config['pooltool']['send_wait'] or height <= self.__last_height:
            return

        try:
            logger.info(f'Sending height {height} to PoolTool')
            self.__session.get(pooltool_endp + str(height)).raise_for_status()
            self.__last_sent = time()
            self.__last_height = height
        except:
            logger.error('Cannot connect to PoolTool')

    def majority_max(self):
        """Return majority max value from PoolTool, or last known value if unavailable
        """
        if time() - self.__last_recv >= config['pooltool']['recv_wait']:
            try:
                resp = self.__session.get(config['pooltool']['endp_stats'])
                resp.raise_for_status()
                self.__majority_max = int(resp.json()['majoritymax'])
                self.__last_recv = time()
            except:
                logger.warning(f"Couldn't update majority max from PoolTool, using last known value {self.__majority_max}")

        return self.__majority_max


def one_best_leader(runners):
    """Make sure there is exactly one best behaving leader if possible
    """
    heights = [r.height() for r in runners]
    leaders = [r.is_leader() for r in runners]
    max_height = max(heights)

    # demote poorly behaving leaders
    for ix, r in enumerate(runners):
        if leaders[ix] and heights[ix] < max_height - 1:
            r.demote()
            leaders[ix] = False

    cnt_leaders = len(leaders)
    if cnt_leaders == 1:
        return

    chosen_one = heights.index(max(heights))

    if cnt_leaders > 1:
        logger.warning(f'Multiple leaders present ({cnt_leaders}), keeping only #{chosen_one}')
        for ix, r in enumerate(runners):
            if r.is_leader() and ix != chosen_one:
                r.demote()
    elif runners[chosen_one].status() == Status.ON:
        runners[chosen_one].promote()


def handle_near_events(runners, stats, events, epoch_end_time):
    """Prepare for the upcoming events:

      - stop the bootstrapping runners (dont risk adversarial forks) 30 seconds ahead
      - if the event is an epoch rollover, kill all runners except one
      - sleep until it is over
    """
    if not events:
        # Leave only one runner without known events
        if len([s != Status.OFF for s in stats]) > 1:
            leave_ix = stats.index(Status.ON) if Status.ON in stats else stats.index(Status.BOOT)
            for ix, r in enumerate(runners):
                if stats[ix] != Status.OFF and ix != leave_ix:
                    r.stop()
        return

    curr_time = time()
    closest_event = min([e for e in events if e > curr_time])
    time_remaining = closest_event - time()

    # Stop bootstrapping runners in advance before event
    if time_remaining < config['event_boot_stop'] and Status.BOOT in stats:
        # Leave one if none other are on
        leave_booting_ix = -1
        if not Status.ON in stats:
            leave_booting_ix = stats.index(Status.BOOT) if Status.BOOT in stats else -1

        logger.info(f'Event ahead in {time_remaining} seconds, killing bootstrapping runners')
        for ix, r in enumerate(runners):
            if stats[ix] == Status.BOOT and ix != leave_booting_ix:
                r.stop()

    # Kill all runners except one leader before epoch rollover
    if epoch_end_time and epoch_end_time - curr_time < config['epoch_kill']:
        leader_seen = False
        for r in runners:
            if not leader_seen and r.is_leader():
                leader_seen = True
            else:
                r.stop()

    # Hibernate if the event is really close
    if time_remaining < config['event_hibernate']:
        logger.info(f'Preparing for a close event in {time_remaining} seconds, hibernating')
        sleep(time_remaining + 2)
        logger.info(f'Woke up')


def safe_to_start(events):
    """Check if it is a safe time to start other runners
    """
    # Without knowing the events schedule it is never safe
    if not events:
        return False

    curr_time = time()

    for event_time in events:
        if curr_time < event_time and event_time - curr_time < config['start_before_event']:
            return False
    return True


def main():
    runners = [Runner(i) for i in range(config['cnt_runners'])]
    pooltool = PoolTool()

    block_0_time = None
    slot_duration = None
    slots_per_epoch = None

    epoch = None
    epoch_end_time = None

    leader_events = []
    events = []

    while True:
        # Update auxiliary variables
        heights = [r.height() for r in runners]
        pt_major_max = pooltool.majority_max()
        stats = [r.status() for r in runners]
        # get index of any running (not bootstrapping) runner
        runner_on = stats.index(Status.ON) if Status.ON in stats else None

        # If all runners are off, start one
        if all([s == Status.OFF for s in stats]):
            logger.info(f'All Jormungandr runners are off, starting one')
            runners[0].restart()
            stats[0] = runners[0].status()

        # Get settings values
        if not epoch_end_time and runner_on is not None:
            block_0_time = block_0_time or runners[runner_on].block_0_time()
            slot_duration = slot_duration or runners[runner_on].slot_duration()
            slots_per_epoch = slots_per_epoch or runners[runner_on].slots_per_epoch()
            epoch = int((time() - block_0_time) / (slot_duration * slots_per_epoch))
            logger.info(f'The current epoch is {epoch}')
            epoch_end_time = (epoch + 1) * slot_duration * slots_per_epoch + block_0_time - 1
            logger.info(f'The current epoch ends at {epoch_end_time}')

        # Get schedule of leader events
        if not leader_events and runner_on is not None:
            leader_events = runners[runner_on].leader_events()

        # Handle near events:
        events = leader_events if leader_events else []
        events += [epoch_end_time] if epoch_end_time else []
        handle_near_events(runners, stats, events, epoch_end_time)

        # Make sure there is exactly one best behaving leader if possible
        one_best_leader(runners)

        # Set boot_end_time to nodes that just turned the state to ON
        for ix, r in enumerate(runners):
            if stats[ix] == Status.ON and r.booting:
                r.booting = False
                r.boot_end_time = time()

        # Restart stuck runners
        for ix, r in enumerate(runners):
            # if the height difference from known maximum exceeded threshold
            known_max = max(heights + [pt_major_max])
            if stats[ix] == Status.ON and heights[ix] + config['max_offset'] < known_max and safe_to_start(events) and time() - r.boot_end_time > config['boot_catch_up']:
                logger.info(f'Jormungandr runner {ix} is stuck, local: {heights[ix]}, known max: {known_max}')
                r.restart()

            # if the bootstrap process is taking too long
            if stats[ix] == Status.BOOT and time() - r.boot_start_time > config['max_boot']:
                r.restart(force=True)

        # Reset variables after epoch rollover
        if epoch_end_time and time() > epoch_end_time:
            epoch = None
            epoch_end_time = None
            leader_events = None

        # Start the rest of the runners if we know for sure there is enough time.
        # Only possible with known leader events (and at least one assigned block).
        if Status.OFF in stats and leader_events and safe_to_start(events):
            for ix, r in enumerate(runners):
                if stats[ix] == Status.OFF:
                    r.restart()

        # Report max height to PoolTool
        pooltool.send_height(max(heights))

        # Remove passed events
        leader_events = [e for e in leader_events if e > time()]

        # Wait before next cycle
        sleep(3)


if __name__ == '__main__':
    main()
