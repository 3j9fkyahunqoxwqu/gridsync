# -*- coding: utf-8 -*-

import ConfigParser
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import time
import urllib2

from twisted.internet import reactor
from twisted.internet.defer import gatherResults
from twisted.internet.threads import deferToThread, blockingCallFromThread

from gridsync.util import h2b, b2h


DEFAULT_SETTINGS = {
    "node": {
        "web.port": "tcp:0:interface=127.0.0.1"
    },
    "client": {
        "shares.happy": "1",
        "shares.total": "1",
        "shares.needed": "1"
    },
    "sync": {}
}


def bin_tahoe():
    for path in os.environ["PATH"].split(os.pathsep):
        filepath = os.path.join(path, 'tahoe')
        if os.path.isfile(filepath):
            return filepath


class Tahoe():
    def __init__(self, node_dir=None, settings=None):
        if not node_dir:
            self.node_dir = os.path.join(os.path.expanduser('~'), '.tahoe')
        else:
            self.node_dir = os.path.expanduser(node_dir)
        self.settings = settings
        self.name = os.path.basename(self.node_dir)
        self.status = {}
        self.node_url = None
        if not os.path.isdir(self.node_dir):
            self.command(['create-client'])
        if self.settings:
            self.setup(settings)

    def get_config(self, section, option):
        config = ConfigParser.RawConfigParser(allow_no_value=True)
        config.read(os.path.join(self.node_dir, 'tahoe.cfg'))
        return config.get(section, option)

    def set_config(self, section, option, value):
        logging.debug("Setting {} option {} to: {}".format(
            section, option, value))
        config = ConfigParser.RawConfigParser(allow_no_value=True)
        config.read(os.path.join(self.node_dir, 'tahoe.cfg'))
        config.set(section, option, value)
        with open(os.path.join(self.node_dir, 'tahoe.cfg'), 'wb') as f:
            config.write(f)

    def setup(self, settings):
        for section, d in settings.iteritems():
            for option, value in d.iteritems():
                if section != 'sync':
                    self.set_config(section, option, value)

    def command(self, args, quiet=False, num_attempts=1):
        full_args = ['tahoe', '-d', self.node_dir] + args
        env = os.environ
        env['PYTHONUNBUFFERED'] = '1'
        if not quiet:
            logging.debug("Running: {}".format(' '.join(full_args)))
        proc = subprocess.Popen(full_args, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, universal_newlines=True)
        output = ''
        for line in iter(proc.stdout.readline, ''):
            if not quiet:
                logging.debug("[pid:{}] {}".format(proc.pid, line.rstrip()))
            output = output + line
        proc.poll()
        if proc.returncode:
            logging.error("pid {} ({}) excited with code {}".format(
                    proc.pid, ' '.join(full_args), proc.returncode))
            num_attempts -= 1
            if num_attempts:
                logging.debug("Trying again ({} attempts remaining)...".format(
                    num_attempts))
                time.sleep(1)
                self.command(args, quiet, num_attempts)
            else:
                raise RuntimeError(output.rstrip())
        elif proc.returncode is None:
            logging.warning("No return code for pid:{} ({})".format(
                    proc.pid, ' '.join(full_args)))
        elif not quiet:
                logging.debug("pid {} ({}) excited with code {}".format(
                        proc.pid, ' '.join(full_args), proc.returncode))
        return output.rstrip()

    def start(self):
        if not os.path.isfile(os.path.join(self.node_dir, 'twistd.pid')):
            self.command(['start'])
        else:
            pid = int(open(os.path.join(self.node_dir, 'twistd.pid')).read())
            try:
                os.kill(pid, 0)
            except OSError:
                self.command(['start'])
        self.node_url = open(os.path.join(self.node_dir, 'node.url')).read()\
                .strip()
        logging.debug("Node URL is: {}".format(self.node_url))

    def update_status(self):
        # https://tahoe-lafs.org/trac/tahoe-lafs/ticket/2476
        html = urllib2.urlopen(self.node_url).read()
        p = re.compile("Connected to <span>(.+?)</span>")
        self.status['servers_connected'] = int(re.findall(p, html)[0])
        p = re.compile("of <span>(.+?)</span> known storage servers")
        self.status['servers_known'] = int(re.findall(p, html)[0])

        servers = {}
        p = re.compile('<div class="nodeid">(.+?)</div>')
        nodeid = re.findall(p, html)
        for item in nodeid:
            servers[item] = {}

        def insert_all(s, tag='td'):
            p = re.compile('<{} class="{}">(.+?)</{}>'.format(tag, s, tag))
            for index, item in enumerate(re.findall(p, html)):
                key = s.replace('service-', '').replace('-', '_').replace(' ', '_')
                servers[nodeid[index]][key] = item

        insert_all('nickname', 'div')
        insert_all('address')
        insert_all('service-service-name')
        insert_all('service-since timestamp')
        insert_all('service-announced timestamp')
        insert_all('service-version')
        insert_all('service-available-space')

        p = re.compile('<div class="furl">(.+?)</div>')
        r = re.findall(p, html)
        self.status['introducer'] = { 'furl': r[0] }
        self.status['helper'] = { 'furl': r[1] }
        self.status['servers'] = servers

        p = re.compile('<div class="status-indicator">(.+?)</div>')
        l = re.findall(p, html)
        for index, item in enumerate(l):
            p = re.compile('alt="(.+?)"')
            status = re.findall(p, item)[0]
            if index == 0:
                self.status['introducer']['status'] = status
            elif index == 1:
                self.status['helper']['status'] = status
            else:
                self.status['servers'][nodeid[index - 2]]['status'] = status
        total_available_space = 0
        for _, v in self.status['servers'].iteritems():
            try:
                total_available_space += h2b(v['available_space'])
            except ValueError:
                pass
        self.status['total_available_space'] = b2h(total_available_space)

    def stored(self, filepath, size=None, mtime=None):
        """Return filecap if filepath has been stored previously via backup"""
        if not size:
            try:
                size = os.path.getsize(filepath)
            except OSError:
                return
        if not mtime:
            try:
                mtime = int(os.path.getmtime(filepath))
            except OSError:
                return
        db = os.path.join(self.node_dir, 'private', 'backupdb.sqlite')
        if not os.path.isfile(db):
            return
        connection = sqlite3.connect(db)
        with connection:
            try:
                cursor = connection.cursor()
                cursor.execute('SELECT "filecap" FROM "caps" WHERE fileid=('
                        'SELECT "fileid" FROM "local_files" WHERE path=? '
                        'AND size=? AND mtime=?)', (filepath, size, mtime))
                return cursor.fetchone()[0]
            except TypeError:
                return

    def get_aliases(self):
        aliases = {}
        aliases_file = os.path.join(self.node_dir, 'private', 'aliases')
        try:
            with open(aliases_file) as f:
                for line in f.readlines():
                    if not line.startswith('#'):
                        try:
                            name, cap = line.split(':', 1)
                            aliases[name + ':'] = cap.strip()
                        except ValueError:
                            pass
            return aliases
        except IOError:
            return

    def get_dircap_from_alias(self, alias):
        if not alias.endswith(':'):
            alias = alias + ':'
        try:
            for name, cap in self.get_aliases().iteritems():
                if name == alias:
                    return cap
        except AttributeError:
            return

    def get_alias_from_dircap(self, dircap):
        try:
            for name, cap in self.get_aliases().iteritems():
                if cap == dircap:
                    return name
        except AttributeError:
            return

    def aliasify(self, dircap_or_alias):
        """Return a valid alias corresponding to a given dircap or alias.

        Return the alias of the given dircap, if it already exists, or, if
        it doesn't, add and return an alias using the SHA-256 hash of the
        dircap as the name.

        If an alias is passed instead of a dircap, return it only if it
        corresponds to a valid dircap, raising a ValueError if the dircap
        is invalid, or a LookupError if it is missing from the aliases file.
        """
        if dircap_or_alias.startswith('URI:DIR'):
            alias = self.get_alias_from_dircap(dircap_or_alias)
            if alias:
                return alias
            else:
                hash_of_dircap = hashlib.sha256(dircap_or_alias).hexdigest()
                self.command(['add-alias', hash_of_dircap, dircap_or_alias])
                return self.get_alias_from_dircap(dircap_or_alias)
        else:
            dircap = self.get_dircap_from_alias(dircap_or_alias)
            if dircap:
                if dircap.startswith('URI:DIR'):
                    return dircap_or_alias
                else:
                    raise ValueError('Invalid alias for {} ({})'.format(
                        dircap_or_alias, dircap))
            #else:
            #    self.command(['create-alias', dircap_or_alias])
            #    return dircap_or_alias
            else:
                raise LookupError('No dircap found for alias {}'.format(
                    dircap_or_alias))

    def get_latest_snapshot(self, dircap):
        received_data = self.command(['ls', dircap + "Archives"], quiet=True)
        return received_data.split('\n')[-1]

    def get_metadata(self, dircap, basedir=''):
        metadata = {}
        jobs = []
        logging.debug("Getting remote metadata from {}...".format(dircap))
        received_data = json.loads(self.command(['ls', '--json', dircap],
                quiet=True))
        for filename, data in received_data[1]['children'].iteritems():
            path = '/'.join([basedir, filename]).strip('/')
            metadata[path] = {
                'uri': data[1]['ro_uri'],
                'mtime': int(data[1]['metadata']['mtime']),
            }
            if data[0] == 'dirnode':
                jobs.append(deferToThread(self.get_metadata,
                    '/'.join([dircap, filename]), path))
        results = blockingCallFromThread(reactor, gatherResults, jobs)
        for result in results:
            metadata.update(result)
        return metadata

