# jorm_master
Jormungander master for running multiple node instances.

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
3. Reload SystemD daemon
   ```
   # systemctl daemon-reload
   ```
4. Start the jorm_master
   ```
   # systemctl enable --now jorm_master.service
   ```
5. Monitor the logs
   ```
   $ journalctl -u jorm_master.service -f
   ```

## Notes

This setup with runners as SystemD units allows restarting the manager without having a downtime. The master will detect the runners on startup and continue with normal operations. This can be usefull on jorm_manager updates, or adjustments of the values in the config file.

| If the number of runners is decreased, it is users responsibility to stop the remaining runners over the limit.
