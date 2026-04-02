#!/bin/bash
tar -xvf sdthd.tar 
cp sdthd /usr/libexec/vaisala/pipes
cp sdthd.sh /usr/libexec/vaisala/pipes
cp sdthd.conf /etc/vaisala/irisrda
chown operator:users /etc/vaisala/irisrda/sdthd.conf
chmod 775 /usr/libexec/vaisala/pipes/sdthd*

