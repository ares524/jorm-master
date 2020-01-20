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
    """Current status of a runner
    """
    OFF = 1
    BOOT = 2  # Bootstrapping
    ON = 3


class Runner:
    def __init__(self, id):
        self.booting = False
        self.boot_start_time = time()  # don't kill booting runners after jorm_manager restart
        self.boot_end_time = 0
        self.__id = id
        self.__session = requests.Session()
        self.__rest = f'http://127.0.0.1:{config["rest_prefix"]}{id}'
        # caching of status and height results
        self.__status = None
        self.__height = None
        self.__status_time = 0
        self.__height_time = 0

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
            # Update cache if necessary
            if self.__status is None or time() - self.__status_time > 2:
                self.__status = Status.ON if self.__node_stats()['state'] == 'Running' else Status.BOOT
                self.__status_time = time()
        except:
            # Make sure the service is really stopped (REST API issues)
            if run(['systemctl', 'is-active', '--quiet', f'jorm_runner@{self.__id}']).returncode == 0:
                logger.error(f'Jormungandr runner {self.__id} not responding to REST api requests, stopping')
                self.stop()
            self.__status = Status.OFF
            self.__status_time = time()

        return self.__status

    def height(self):
        """Return current block height, or 0 if unavailable
        """
        try:
            # Update cache if necessary
            if self.__height is None or time() - self.__height_time > 2:
                self.__height = int(self.__node_stats()['lastBlockHeight'])
                self.__height_time = time()
        except:
            self.__height = 0
            self.__height_time = time()

        return self.__height

    def restart(self):
        """(Re)start Jormungandr runner
        """
        logger.info(f'(Re)starting Jormungandr runner {self.__id}')
        run(['systemctl', 'restart', f'jorm_runner@{self.__id}.service'])
        self.boot_start_time = time()
        self.booting = True
        self.__status_time = 0  # expire cache

    def stop(self):
        """Stop Jormungandr runner
        """
        logger.info(f'Stopping Jormungandr runner {self.__id}')
        run(['systemctl', 'stop', f'jorm_runner@{self.__id}.service'])
        self.booting = False
        self.__status_time = 0  # expire cache

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
        try:
            events = self.__session.get(f'{self.__rest}/api/v0/leaders/logs').json()
            return [unix_time(e['scheduled_at_time']) for e in events]
        except:
            return []

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


class Master:
    def __init__(self, cnt_runners):
        self.__runners = [Runner(i) for i in range(cnt_runners)]
        self.__block_0_time = None
        self.__slot_duration = None
        self.__slots_per_epoch = None
        self.__epoch = None
        self.__epoch_end_time = None
        self.__leader_events = []

    def settings_loaded(self):
        """From epoch_end_time determine if the settings for current epoch are loaded
        """
        return self.__epoch_end_time is not None

    def load_settings(self):
        """Load general settings and compute epoch and epoch_end_time
        """
        runner_on = None
        for r in self.__runners:
            if r.status() == Status.ON:
                runner_on = r
                break
        if runner_on == None:
            return

        if self.__block_0_time is None:
            self.__block_0_time = runner_on.block_0_time()
        if self.__slot_duration is None:
            self.__slot_duration = runner_on.slot_duration()
        if self.__slots_per_epoch is None:
            self.__slots_per_epoch = runner_on.slots_per_epoch()
        if self.__epoch is None:
            self.__epoch = int((time() - self.__block_0_time) / (self.__slot_duration * self.__slots_per_epoch))
            logger.info(f'The current epoch is {self.__epoch}')
        if self.__epoch_end_time is None:
            self.__epoch_end_time = (self.__epoch + 1) * self.__slot_duration * self.__slots_per_epoch + self.__block_0_time - 1
            logger.info(f'The current epoch ends at {self.__epoch_end_time}')

    def __upcoming_events(self):
        """Return list of upcoming events
        """
        return [e for e in self.__leader_events if e > time()]

    def __closest_event(self):
        """Return time remaining before next event
        """
        upcoming_events = self.__upcoming_events()
        return min(upcoming_events) if upcoming_events else None

    def __log_events(self):
        """Write to logs schedule of upcoming events
        """
        events = sorted(self.__upcoming_events())
        for ix, e in enumerate(events):
            logger.info(f'Upcoming event: {ix} at {datetime.fromtimestamp(e)}')

    def cnt_events(self, only_future=True):
        """Return number of events scheduled
        """
        return len(self.__upcoming_events()) if only_future else len(self.__leader_events)

    def load_events(self):
        """Load leader events
        """
        for r in self.__runners:
            if r.status() == Status.ON:
                self.__leader_events = r.leader_events()
                self.__log_events()
                return

    def stats(self):
        """Return list with status of each runner
        """
        return [r.status() for r in self.__runners]

    def heights(self):
        """Return list with block heights of each runner
        """
        return [r.height() for r in self.__runners]

    def start_runner(self, id):
        """Start a Jormungandr runner
        """
        self.__runners[id].restart()

    def __runners_sorted(self):
        """Return list of runner indexes sorted by their preference (best at index 0)
        """
        # Sort indexes of all runners by their preference
        return sorted(range(len(self.__runners)),
                      key=lambda i: (self.__runners[i].status() == Status.ON,    # 1) is running
                                     self.__runners[i].height(),                 # 2) height
                                     self.__runners[i].is_leader(),              # 3) is leader
                                     self.__runners[i].status() == Status.BOOT), # 4) is bootstrapping
                      reverse=True)

    def one_runner(self):
        """Leave only one best behaving runner
        """
        leave_ix = self.__runners_sorted()[0]

        for ix, r in enumerate(self.__runners):
            if ix != leave_ix and r.status() != Status.OFF:
                r.stop()

        r = self.__runners[leave_ix]
        if r.status() == Status.ON and not r.is_leader():
            r.promote()

    def handle_near_events(self):
        """Prepare for the upcoming events:

        - 1. phase: stop the bootstrapping runners (dont risk adversarial forks)
        - 2. phase: hibernate until the event is over
        - epoch rollover: kill all runners except one
        """
        if self.cnt_events() > 0:
            time_remaining = self.__closest_event() - time()
            stats = self.stats()

            # Stop bootstrapping runners in advance before event
            if time_remaining < config['event_boot_stop'] and Status.BOOT in self.stats():
                # Leave one if none other are on
                leave_booting_ix = -1
                if Status.BOOT in stats and not Status.ON in stats:
                    leave_booting_ix = stats.index(Status.BOOT)

                logger.info(f'Event ahead in {time_remaining:.2f} seconds, killing bootstrapping runners')
                for ix, r in enumerate(self.__runners):
                    if stats[ix] == Status.BOOT and ix != leave_booting_ix:
                        r.stop()

        # Kill all runners except one leader before epoch rollover
        if self.__epoch_end_time and self.__epoch_end_time - time() < config['epoch_kill']:
            self.one_runner()

        # Hibernate if the event is really close
        if time_remaining < config['event_hibernate']:
            logger.info(f'Preparing for a close event in {time_remaining:.2f} seconds, hibernating')
            sleep(time_remaining + 2)
            logger.info(f'Woke up')

    def best_leader(self):
        """Make sure there is exactly one best behaving leader if possible
        """
        best_ix = self.__runners_sorted()[0]

        for ix, r in enumerate(self.__runners):
            if ix != best_ix and r.is_leader():
                r.demote()

        if not self.__runners[best_ix].is_leader():
            self.__runners[best_ix].promote()

    def set_boot_times(self):
        """Set boot_end_time for nodes that just turned their state to ON
        """
        for r in self.__runners:
            if r.status() == Status.ON and r.booting:
                r.booting = False
                r.boot_end_time = time()
            # Handle manual runner restarts by user
            if r.status() == Status.BOOT and not r.booting:
                r.booting = True
                r.boot_start_time = time()

    def __safe_to_start(self):
        """Is it a safe time to start a new runner?
        """
        # Without knowing the events schedule it is never safe
        if self.cnt_events(only_future=False) == 0 or self.__epoch_end_time is None:
            return False

        return self.__closest_event() - time() > config['start_before_event']

    def restart_stuck(self, pt_max):
        """Restart stuck runners
        """
        known_max = max(self.heights() + [pt_max])

        for ix, r in enumerate(self.__runners):
            # if the height difference from known maximum exceeded threshold
            is_stuck = r.status() == Status.ON and known_max - r.height() > config['max_offset']
            if is_stuck and self.__safe_to_start() and time() - r.boot_end_time > config['boot_catch_up']:
                logger.info(f'Jormungandr runner {ix} is stuck, local: {r.height()}, known max: {known_max}')
                r.restart()

            # if the bootstrap process is taking too long
            if r.status() == Status.BOOT and time() - r.boot_start_time > config['max_boot']:
                r.restart()

    def reset_on_epoch_end(self):
        """Clear no longer valid variables after epoch rollover
        """
        if self.__epoch_end_time and time() > self.__epoch_end_time:
            self.__epoch = None
            self.__epoch_end_time = None
            self.__leader_events = None

    def start_if_possible(self):
        """ Start the rest of the runners if we know for sure there is enough time.
        """
        if self.__safe_to_start():
            for r in self.__runners:
                if r.status() == Status.OFF:
                    r.restart()


def main():
    master = Master(cnt_runners=config['cnt_runners'])
    pooltool = PoolTool()

    while True:
        # Start first runner if all of them are stopped
        if all([s == Status.OFF for s in master.stats()]):
            logger.info(f'All Jormungandr runners are off, starting one')
            master.start_runner(id=0)

        # Load genesis settings
        if not master.settings_loaded():
            master.load_settings()

        # Get leader events
        if master.cnt_events(only_future=False) == 0:
            master.load_events()

        # Without known events leave only one runner
        if master.cnt_events(only_future=False) == 0:
            master.one_runner()

        # Handle near events:
        master.handle_near_events()

        # Make sure there is exactly one best behaving leader if possible
        master.best_leader()

        # Set boot_end_time to nodes that just turned the state to ON
        master.set_boot_times()

        # Restart stuck runners
        pt_max = pooltool.majority_max()
        master.restart_stuck(pt_max)

        # Reset variables after epoch rollover
        master.reset_on_epoch_end()

        # Start the rest of the runners if we know for sure there is enough time.
        master.start_if_possible()

        # Report max height to PoolTool
        pooltool.send_height(max(master.heights()))

        # Wait before next cycle
        sleep(3)


if __name__ == '__main__':
    main()
