#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import logging
import socket
import sys

from twisted.internet.error import CannotListenError

from gridsync import __doc__ as description
from gridsync._version import __version__
from gridsync.core import Core


class TahoeVersion(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        import subprocess
        subprocess.call(['tahoe', '--version-and-path'])
        sys.exit()


def main():
    parser = argparse.ArgumentParser(
        description=description)
    parser.add_argument(
        'command',
        nargs='*',
        help='Command to send (e.g., "quit").')
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Print debug messages to STDOUT.')
    parser.add_argument(
        '-g',
        '--no-gui',
        action='store_true',
        help='Run without GUI.')
    parser.add_argument(
        '--tahoe-version',
        nargs=0,
        action=TahoeVersion,
        help="Call 'tahoe --version-and-path' and exit. For debugging.")
    parser.add_argument(
        '-V',
        '--version',
        action="version",
        version='%(prog)s ' + __version__)
    args = parser.parse_args()

    try:
        core = Core(args)
        core.start()
    except CannotListenError:
        if args.command:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(("localhost", 52045))
                s.send(' '.join(args.command).encode())
                return 0
            except OSError as err:
                logging.error("Error sending command(s) '%s': %s",
                              ' '.join(args.command), str(err))
                return 1
        else:
            logging.error("Gridsync already running.")
            return 1


if __name__ == "__main__":
    sys.exit(main())
