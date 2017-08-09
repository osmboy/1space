#!/bin/bash

set -e

# Make sure all of the .pid files are removed -- services will not start
# otherwise
find /var/lib/ -name *.pid -delete

# Copied from the docker swift container. Unfortunately, there is no way to
# plugin an additional invocation to start swift-s3-sync, so we had to do this.
/usr/sbin/service rsyslog start
/usr/sbin/service rsync start
/usr/sbin/service memcached start
# set up storage
mkdir -p /swift/nodes/1 /swift/nodes/2 /swift/nodes/3 /swift/nodes/4
ln -s /swift/nodes/1 /srv/1
ln -s /swift/nodes/2 /srv/2
ln -s /swift/nodes/3 /srv/3
ln -s /swift/nodes/4 /srv/4
mkdir -p /srv/1/node/sdb1 /srv/2/node/sdb2 /srv/3/node/sdb3 /srv/4/node/sdb4 \
    /var/run/swift
/usr/bin/sudo /bin/chown -R swift:swift /swift/nodes /etc/swift /srv/1 /srv/2 \
    /srv/3 /srv/4 /var/run/swift
/usr/bin/sudo -u swift /swift/bin/remakerings

# stick cloud sync shunt into the proxy pipeline
sed -i 's/tempurl tempauth/& cloud_sync_shunt/' /etc/swift/proxy-server.conf
cat <<EOF >> /etc/swift/proxy-server.conf
[filter:cloud_sync_shunt]
use = call:s3_sync.shunt:filter_factory
conf_file = /swift-s3-sync/test/container/swift-s3-sync.conf
EOF

/usr/bin/sudo -u swift PYTHONPATH=/swift-s3-sync /swift/bin/startmain

PYTHONPATH=/opt/ss/lib/python2.7/dist-packages:/swift-s3-sync \
    python -m s3_sync --log-level debug \
    --config /swift-s3-sync/test/container/swift-s3-sync.conf &

/bin/bash /s3proxy/s3proxy \
    --properties /swift-s3-sync/test/container/s3proxy.properties \
    2>&1 > /var/log/s3proxy.log &

/usr/local/bin/supervisord -n -c /etc/supervisord.conf
