# jorm_master
Jormungander master for running multiple node instances with no adversarial forks guarantee.

## Setup

1. Configure the jorm_master in [jorm_master.yaml](jorm_master.yaml) file
   - adjust also other files if needed
2. Move files to the correct location on the server
   - [jorm_master.py](jorm_master.py) -> `/usr/local/bin/`
   - [jorm_master.service](jorm_master.service) -> `/etc/systemd/system/`
   - [jorm_master.yaml](jorm_master.yaml) -> `/etc/cardano/`
   - [jorm_runner.sh](jorm_runner.sh) -> `/usr/local/bin/`
   - [jorm_runner@.service](jorm_runner@.service) -> `/etc/systemd/system/`
   - `jorm_runner_N.yaml` -> `/etc/cardano/` for each runner (0 to cnt - 1)
3. Create a symbolic link to the Jormungandr executable for each runner
   - E.g. with executables in `/usr/local/bin/` suffixed by a commit hash
     ```
     # ln -s /usr/local/bin/jormungandr_546497fc /usr/local/bin/jormungandr_runner_0
     ```
4. Reload SystemD daemon
   ```
   # systemctl daemon-reload
   ```
5. Start the jorm_master
   ```
   # systemctl enable --now jorm_master.service
   ```
6. Monitor the logs
   ```
   $ journalctl -u jorm_master.service -f
   ```

## Notes

This setup with runners as SystemD units allows restarting the master without having a downtime. The master will detect the runners on startup and continue with normal operations. This can be usefull on jorm_master updates, or adjustments of the values in the config file.

| If the number of runners is decreased, it is users responsibility to stop the remaining runners over the limit. (`systemctl stop jorm_runner@N.service`).

With the current implementation, the master shouldn't allow any adversarial forks (running multiple runners as a leader during time of any event). However, this guarantee brings a few limitations:
 - During the epoch's rollover all but one runners will be stopped.
 - Without known leader's events only one runner will be used (implies slow cold start).
 - If an event approaches, all bootstrapping runners are stopped.
