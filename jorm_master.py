#!/usr/bin/env python3

# Commander of multiple Jormungander runners for ensuring high availability
#
# author: Tomas Stefan <ts@stdin.cz>

import requests
import logging
import yaml
import re
from time import sleep, time
from systemd.journal import JournaldLogHandler
from enum import IntEnum
from subprocess import run, PIPE
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

# Prepare RegExp for choosing between different time formats
#  - Jormungandr format, e.g. '2019-12-13T19:13:37+00:00'
time_jormungandr = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+[0-9]{2}:[0-9]{2}$')
#  - Systemctl format, e.g. 'Fri 2020-01-24 05:52:48 CET'
time_systemctl = re.compile(r'[A-Z][a-z]{2} [0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} [A-Z]{3}')


def unix_time(time_str):
    """Converts string to unix time
    """
    if time_jormungandr.match(time_str) is not None:
        t = time_str[:22] + time_str[23:]  # get rid of the ':' in time zone
        return int(datetime.strptime(t, '%Y-%m-%dT%H:%M:%S%z').timestamp())
    elif time_systemctl.match(time_str) is not None:
        return int(datetime.strptime(time_str, '%a %Y-%m-%d %H:%M:%S %Z').timestamp())
    else:
        err = f'Cannot convert "{time_str}" to unix time'
        logger.error(err)
        raise ValueError(err)


class Status(IntEnum):
    """Current status of a runner
    """
    OFF = 1   # Shut down
    BOOT = 2  # Bootstrapping
    ON = 3    # Running


class Runner:
    def __init__(self, id):
        self.id = id
        self.__session = requests.Session()
        self.__rest = f'http://127.0.0.1:{config["rest_prefix"]}{id}'
        # caching of status and height results
        self.__status = None
        self.__height = None
        self.__uptime = None
        self.__status_updated_time = 0
        self.__height_updated_time = 0
        self.__uptime_updated_time = 0

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
        except Exception:
            return None

    def status(self):
        """Return runner's status - ON/BOOT/OFF
        """
        if self.__status is None or time() - self.__status_updated_time > 2:
            # Update cached value
            if run(['systemctl', 'is-active', '--quiet', f'jorm_runner@{self.id}']).returncode == 0:
                failures = 0
                while True:
                    try:
                        status = self.__node_stats()['state']
                        if status == 'Running':
                            self.__status = Status.ON
                        elif status in ['Bootstrapping', 'PreparingBlock0']:
                            self.__status = Status.BOOT
                        else:
                            raise ValueError("Cannot decide runner's status")
                        break
                    except Exception:
                        failures += 1
                        if failures >= 2:
                            logger.error(f'Jormungandr runner {self.id} is not responding to REST api requests, stopping')
                            self.stop()
                            self.__status = Status.OFF
                            break
            else:
                self.__status = Status.OFF

            self.__status_updated_time = time()

        return self.__status

    def height(self):
        """Return current block height, or 0 if unavailable
        """
        try:
            # Update cache if necessary
            if self.__height is None or time() - self.__height_updated_time > 2:
                self.__height = int(self.__node_stats()['lastBlockHeight'])
                self.__height_updated_time = time()
        except Exception:
            self.__height = 0
            self.__height_updated_time = time()

        return self.__height

    def uptime(self):
        """Return current node uptime, or 0 if unavailable
        """
        try:
            # Update cache if necessary
            if self.__uptime is None or time() - self.__uptime_updated_time > 2:
                self.__uptime = int(self.__node_stats()['uptime'])
                self.__uptime_updated_time = time()
        except Exception:
            self.__uptime = 0
            self.__uptime_updated_time = time()

        return self.__uptime

    def service_uptime(self):
        """Return current uptime of the runner service (from systemctl)
        """
        try:
            time_str = run(['systemctl', 'show', f'jorm_runner@{self.id}.service', '--property=ActiveEnterTimestamp'],
                           stdout=PIPE).stdout.decode('utf-8').split('=')[1].strip()
            return time() - unix_time(time_str)
        except Exception:
            logger.warning(f'Cannot read uptime (systemctl) of a runner {self.id}')
            return 0

    def restart(self):
        """(Re)start Jormungandr runner
        """
        logger.warning(f'(Re)starting Jormungandr runner {self.id}')
        run(['systemctl', 'restart', f'jorm_runner@{self.id}.service'])
        self.__status_updated_time = 0  # expire cache

    def stop(self):
        """Stop Jormungandr runner
        """
        logger.info(f'Stopping Jormungandr runner {self.id}')
        run(['systemctl', 'stop', f'jorm_runner@{self.id}.service'])
        self.__status_updated_time = 0  # expire cache

    def suspend(self):
        """Suspend Jormungandr runner
        """
        logger.info(f'Suspending Jormungandr runner {self.id}')
        run(['systemctl', 'kill', '--signal=SIGSTOP', f'jorm_runner@{self.id}.service'])

    def resume(self):
        """Resume suspended Jormungandr runner
        """
        logger.info(f'Resuming Jormungandr runner {self.id}')
        run(['systemctl', 'kill', '--signal=SIGCONT', f'jorm_runner@{self.id}.service'])

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
        except Exception:
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
        except Exception:
            return []

    def promote(self):
        """Make passive node a leader
        """
        try:
            logger.info(f'Promoting Jormungandr runner {self.id} to leader')
            with open(config['node_secret'], 'r') as f:
                secret = yaml.safe_load(f)
                self.__session.post(f'{self.__rest}/api/v0/leaders', json=secret).raise_for_status()
        except Exception:
            logger.error(f'Cannot promote Jormungandr runner {self.id} to leader')

    def demote(self):
        """Make the runner a passive node without the possibility to create blocks
        """
        try:
            for leader_id in self.leader_ids():
                logger.info(f'Removing leader id {leader_id} from Jormungandr runner {self.id}')
                self.__session.delete(f'{self.__rest}/api/v0/leaders/{leader_id}').raise_for_status()
        except Exception:
            logger.error(f'Cannot demote Jormungandr runner {self.id}')


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
        except Exception:
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
            except Exception:
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
        self.__epoch_events_known = False

    def settings_loaded(self):
        """From epoch_end_time determine if the settings for current epoch are loaded
        """
        if self.__epoch_end_time is None:
            return False
        elif self.__epoch_end_time < time():
            self.__epoch_events_known = False
            self.__epoch = None
            self.__epoch_end_time = None
            return False
        else:
            return True

    def load_settings(self):
        """Load general settings and compute epoch and epoch_end_time
        """
        runner_on = None
        for r in self.__runners:
            if r.status() == Status.ON:
                runner_on = r
                break
        if runner_on is None:
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
            logger.info(f'The current epoch {self.__epoch} ends at {datetime.fromtimestamp(self.__epoch_end_time)}')

    def events_known(self):
        return self.__epoch_events_known

    def __upcoming_events(self, epoch_roll=False):
        """Return list of upcoming events
        """
        res = [e for e in self.__leader_events if e > time()]
        if epoch_roll and self.__epoch_end_time is not None:
            res += [self.__epoch_end_time]
        return res

    def __closest_event(self, epoch_roll=False):
        """Return time of the closest upcoming event, or None if unavailable
        """
        upcoming_events = self.__upcoming_events(epoch_roll)
        return min(upcoming_events) if upcoming_events else None

    def __log_leader_events(self):
        """Write to logs schedule of upcoming leader events
        """
        events = sorted(self.__upcoming_events(epoch_roll=False))
        for i, e in enumerate(events):
            logger.info(f'Upcoming event: {i} at {datetime.fromtimestamp(e)}')

    def cnt_events(self, only_future=True, epoch_roll=False):
        """Return number of events scheduled
        """
        return len(self.__upcoming_events(epoch_roll)) if only_future else len(self.__leader_events)

    def load_leader_events(self):
        """Load leader events
        """
        if not self.settings_loaded():
            return

        epoch_start = self.__epoch * self.__slot_duration * self.__slots_per_epoch + self.__block_0_time

        for r in self.__runners:
            if r.status() == Status.ON:
                events = r.leader_events()
                if len(events) == 0:
                    return
                # check if events are from the current epoch
                if epoch_start <= max(events) <= self.__epoch_end_time:
                    self.__leader_events = events
                    self.__epoch_events_known = True
                    self.__log_leader_events()
                    return

    def stats(self):
        """Return list with status of each runner
        """
        return [r.status() for r in self.__runners]

    def heights(self):
        """Return list with block heights of each runner
        """
        return [r.height() for r in self.__runners]

    # def start_runner(self, id):
    #     """Start a Jormungandr runner
    #     """
    #     self.__runners[id].restart()

    def start_stopped_runners(self):
        """Start all runners, that are currently stopped
        """
        for r in self.__runners:
            if r.status() == Status.OFF:
                r.restart()
                sleep(0.5)

    def __runners_sorted(self):
        """Return list of runner indexes sorted by their preference (best at index 0)
        """
        # Sort indexes of all runners by their preference
        return sorted(range(len(self.__runners)),
                      key=lambda i: (self.__runners[i].status() == Status.ON,     # 1) is running
                                     self.__runners[i].height(),                  # 2) height
                                     self.__runners[i].is_leader(),               # 3) is leader
                                     self.__runners[i].status() == Status.BOOT),  # 4) is bootstrapping
                      reverse=True)

    # def one_runner(self):
    #     """Leave only one best behaving runner
    #     """
    #     leave_id = self.__runners_sorted()[0]

    #     for r in self.__runners:
    #         if r.id != leave_id and r.status() != Status.OFF:
    #             r.stop()

    #     r = self.__runners[leave_id]
    #     if r.status() == Status.ON and not r.is_leader():
    #         r.promote()

    def handle_near_events(self):
        """Prepare for the upcoming events
        """
        if self.cnt_events(only_future=True, epoch_roll=True) <= 0:
            return

        time_remaining = self.__closest_event(epoch_roll=True) - time()

        if time_remaining < config['event_action']:
            booting_nodes = [r for r in self.__runners if r.status() == Status.BOOT]
            epoch_rollover = self.__epoch_end_time is not None and self.__epoch_end_time - time() < config['event_action']

            # Suspend bootstrapping runners
            for r in booting_nodes:
                r.suspend()

            # Promote all nodes for epoch rollover
            if epoch_rollover:
                logger.info(f'Preparing for an epoch rollover, promoting all runners')
                for r in self.__runners:
                    if r.status() == Status.ON and not r.is_leader():
                        r.promote()

            # Update remaining time
            time_remaining = self.__closest_event(epoch_roll=True) - time()

            # Sleep through the event
            logger.info(f'Preparing for a close event in {time_remaining:.2f} seconds, hibernating')
            sleep(time_remaining + 2)
            logger.info(f'Woke up')

            for r in booting_nodes:
                r.resume()

            if epoch_rollover:
                self.__epoch_events_known = False
                self.__epoch = self.__epoch + 1
                self.__epoch_end_time = (self.__epoch + 1) * self.__slot_duration * self.__slots_per_epoch + self.__block_0_time - 1
                logger.info(f'Sleeping for additional 20 s after epoch rollover')
                sleep(20)

    def best_leader(self):
        """Make sure there is exactly one best behaving leader if possible
        """
        best_id = self.__runners_sorted()[0]

        for r in self.__runners:
            if r.id != best_id and r.is_leader():
                r.demote()

        if self.__runners[best_id].status() == Status.ON and not self.__runners[best_id].is_leader():
            self.__runners[best_id].promote()

    def __safe_to_start(self):
        """Is it a safe time to start a new runner?
        """
        return self.__closest_event(epoch_roll=True) - time() > config['start_before_event']

    def restart_stuck(self, pt_max):
        """Restart stuck runners
        """
        known_max = max(self.heights() + [pt_max])

        for r in self.__runners:
            # if the height difference from known maximum exceeded threshold
            is_stuck = r.status() == Status.ON and known_max - r.height() > config['max_offset']
            if is_stuck and self.__safe_to_start() and r.uptime() > config['boot_catch_up']:
                logger.warning(f'Jormungandr runner {r.id} is stuck, local: {r.height()}, known max: {known_max}')
                r.restart()
                sleep(0.5)

            # if the bootstrap process is taking too long
            if r.status() == Status.BOOT and r.service_uptime() > config['max_boot']:
                logger.warning(f'Jormungandr runner {r.id} is bootstrapping for too long')
                r.restart()

    # def start_if_possible(self):
    #     """ Start the rest of the runners if we know for sure there is enough time.
    #     """
    #     if self.__safe_to_start():
    #         for r in self.__runners:
    #             if r.status() == Status.OFF:
    #                 r.restart()


def main():
    master = Master(cnt_runners=config['cnt_runners'])
    pooltool = PoolTool()

    while True:
        # Start all runners, that are stopped
        master.start_stopped_runners()

        # Load genesis settings
        if not master.settings_loaded():
            master.load_settings()

        # Get leader events
        if not master.events_known():
            master.load_leader_events()

        # Handle near events:
        master.handle_near_events()

        # Make sure there is exactly one best behaving leader if possible
        master.best_leader()

        # Restart stuck runners
        pt_max = pooltool.majority_max()
        master.restart_stuck(pt_max)

        # Report max height to PoolTool
        pooltool.send_height(max(master.heights()))

        # Wait before next cycle
        sleep(3)


if __name__ == '__main__':
    main()
