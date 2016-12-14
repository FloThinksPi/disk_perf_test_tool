#!/bin/bash

set -e
set -x

if [ -d /tmp/venv ]; then
exit 0
fi

apt-get update
apt-get -y --force-yes install git python-pip virtualenv python-dev

cd /tmp
virtualenv --no-setuptools venv
git clone http://github.com/openstack/oslo.messaging -b stable/mitaka
source /tmp/venv/bin/activate
pip install setuptools
cd oslo.messaging
python setup.py install
pip install eventlet PyYAML oslo.messaging petname redis zmq pika_pool scipy numpy
pip install kombu
