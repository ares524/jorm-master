#!/bin/bash
# Startup script for Jormungandr runner

CFG_PATH=/etc/cardano
CONFIG=jorm_runner.yaml
SECRET=node_secret.yaml

export RUST_BACKTRACE=full

exec jormungandr --genesis-block-hash $(< /etc/cardano/genesis.txt) \
                 --config "${CFG_PATH}/${CONFIG}"                   \
                 --rest-listen "127.0.0.1:310$1"                    \
                 --storage "/tmp/jormungandr_runner_$1"             \
                 --secret "${CFG_PATH}/${SECRET}"
