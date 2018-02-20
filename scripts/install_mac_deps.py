import platform
import subprocess
import sys


version = platform.mac_ver()[0]

if not version:
    sys.exit("Not a Mac or no version detected; exiting")

major = int(version.split('.')[0])
minor = int(version.split('.')[1])

if (major, minor) < (10, 10):
    print("*** Older Mac detected ({}) ***".format(version))
    print("Rebuilding libsodium/PyNaCl...")
    subprocess.call(['pip', 'install', '--ignore-installed', '--no-deps',
                     '--no-binary', 'PyNaCl', 'PyNaCl==1.2.1'])
    print("Downgrading SIP, PyQt5...")
    subprocess.call(['pip', 'install', 'SIP==4.19.2', 'PyQt5==5.8.2'])