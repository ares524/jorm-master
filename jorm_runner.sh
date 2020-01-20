#!/bin/bash
# Startup script for Jormungandr runner

export RUST_BACKTRACE=full

exec jormungandr --genesis-block-hash $(< /etc/cardano/genesis.txt) \
                 --config "/etc/cardano/jorm_runner_${1}.yaml"      \
                 --rest-listen "127.0.0.1:310${1}"                  \
                 --storage "/home/cardano/storage_jorm_runner_${1}" \
                 --secret "/etc/cardano/node_secret.yaml"
