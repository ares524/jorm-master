#!/bin/bash
# Startup script for Jormungandr runner

CFG_PATH=/etc/cardano
CONFIG=jorm_runner.yaml
SECRET=node_secret.yaml
STORAGE="/home/cardano/storage_jorm_runner"

export RUST_BACKTRACE=full

exec jormungandr --genesis-block-hash $(< /etc/cardano/genesis.txt) \
                 --config "${CFG_PATH}/${CONFIG}"                   \
                 --rest-listen "127.0.0.1:310${1}"                  \
                 --storage "${STORAGE}_${1}"                        \
                 --secret "${CFG_PATH}/${SECRET}"
