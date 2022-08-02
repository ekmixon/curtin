# This file is part of curtin. See LICENSE file for copyright and license info.

# This module wraps calls to the mdadm utility for examing Linux SoftRAID
# virtual devices.  Functions prefixed with 'mdadm_' involve executing
# the 'mdadm' command in a subprocess.  The remaining functions handle
# manipulation of the mdadm output.

import os
import re
import shlex
import time

from curtin.block import (
    dev_path,
    dev_short,
    get_holders,
    is_valid_device,
    md_get_devices_list,
    md_get_spares_list,
    sys_block_path,
    zero_file_at_offsets,
)
from curtin.distro import lsb_release
from curtin import (util, udev)
from curtin.log import LOG

NOSPARE_RAID_LEVELS = [
    'linear', 'raid0', '0', 0,
    'container'
]

SPARE_RAID_LEVELS = [
    'raid1', 'stripe', 'mirror', '1', 1,
    'raid4', '4', 4,
    'raid5', '5', 5,
    'raid6', '6', 6,
    'raid10', '10', 10,
]

VALID_RAID_LEVELS = NOSPARE_RAID_LEVELS + SPARE_RAID_LEVELS

#  https://www.kernel.org/doc/Documentation/md.txt
'''
     clear
         No devices, no size, no level
         Writing is equivalent to STOP_ARRAY ioctl
     inactive
         May have some settings, but array is not active
            all IO results in error
         When written, doesn't tear down array, but just stops it
     suspended (not supported yet)
         All IO requests will block. The array can be reconfigured.
         Writing this, if accepted, will block until array is quiessent
     readonly
         no resync can happen.  no superblocks get written.
         write requests fail
     read-auto
         like readonly, but behaves like 'clean' on a write request.

     clean - no pending writes, but otherwise active.
         When written to inactive array, starts without resync
         If a write request arrives then
           if metadata is known, mark 'dirty' and switch to 'active'.
           if not known, block and switch to write-pending
         If written to an active array that has pending writes, then fails.
     active
         fully active: IO and resync can be happening.
         When written to inactive array, starts with resync

     write-pending
         clean, but writes are blocked waiting for 'active' to be written.

     active-idle
       like active, but no writes have been seen for a while (safe_mode_delay).
'''

ERROR_RAID_STATES = [
    'clear',
    'inactive',
    'suspended',
]

READONLY_RAID_STATES = [
    'readonly',
]

READWRITE_RAID_STATES = [
    'read-auto',
    'clean',
    'active',
    'active-idle',
    'write-pending',
]

VALID_RAID_ARRAY_STATES = (
    ERROR_RAID_STATES +
    READONLY_RAID_STATES +
    READWRITE_RAID_STATES
)

# need a on-import check of version and set the value for later reference
''' mdadm version < 3.3 doesn't include enough info when using --export
    and we must use --detail and parse out information.  This method
    checks the mdadm version and will return True if we can use --export
    for key=value list with enough info, false if version is less than
'''
MDADM_USE_EXPORT = lsb_release()['codename'] not in ['precise', 'trusty']

#
# mdadm executors
#


def mdadm_assemble(md_devname=None, devices=[], spares=[], scan=False,
                   ignore_errors=False):
    # md_devname is a /dev/XXXX
    # devices is non-empty list of /dev/xxx
    # if spares is non-empt list append of /dev/xxx
    cmd = ["mdadm", "--assemble"]
    if scan:
        cmd += ['--scan', '-v']
    else:
        valid_mdname(md_devname)
        cmd += [md_devname, "--run"] + devices
        if spares:
            cmd += spares

    try:
        # mdadm assemble returns 1 when no arrays are found. this might not be
        # an error depending on the situation this function was called in, so
        # accept a return code of 1
        # mdadm assemble returns 2 when called on an array that is already
        # assembled. this is not an error, so accept return code of 2
        # all other return codes can be accepted with ignore_error set to true
        scan, err = util.subp(cmd, capture=True, rcs=[0, 1, 2])
        LOG.debug('mdadm assemble scan results:\n%s\n%s', scan, err)
        scan, err = util.subp(['mdadm', '--detail', '--scan', '-v'],
                              capture=True, rcs=[0, 1])
        LOG.debug('mdadm detail scan after assemble:\n%s\n%s',
                  scan, err)
    except util.ProcessExecutionError:
        LOG.warning("mdadm_assemble had unexpected return code")
        if not ignore_errors:
            raise

    udev.udevadm_settle()


def mdadm_create(md_devname, raidlevel, devices, spares=None, container=None,
                 md_name="", metadata=None):
    LOG.debug(
        (
            ('mdadm_create: ' + f'md_name={md_devname} raidlevel={raidlevel} ')
            + f' devices={devices} spares={spares} name={md_name}'
        )
    )


    assert_valid_devpath(md_devname)
    if not metadata:
        metadata = 'default'

    if raidlevel not in VALID_RAID_LEVELS:
        raise ValueError(f'Invalid raidlevel: [{raidlevel}]')

    min_devices = md_minimum_devices(raidlevel)
    devcnt = len(md_get_devices_list(container)) if container else len(devices)
    if devcnt < min_devices:
        err = f'Not enough devices ({devcnt}) '
        err += f'for raidlevel: {str(raidlevel)}'
        err += f' minimum devices needed: {str(min_devices)}'
        raise ValueError(err)

    if spares and raidlevel not in SPARE_RAID_LEVELS:
        err = f'Raidlevel does not support spare devices: {str(raidlevel)}'
        raise ValueError(err)

    (hostname, _err) = util.subp(["hostname", "-s"], rcs=[0], capture=True)

    cmd = [
        "mdadm",
        "--create",
        md_devname,
        "--run",
        f"--homehost={hostname.strip()}",
        f"--raid-devices={devcnt}",
    ]


    if not container:
        cmd.append(f"--metadata={metadata}")
    if raidlevel != 'container':
        cmd.append(f"--level={raidlevel}")

    if md_name:
        cmd.append(f"--name={md_name}")

    if container:
        cmd.append(container)

    for device in devices:
        holders = get_holders(device)
        if len(holders) > 0:
            LOG.warning('Detected holders during mdadm creation: %s', holders)
            raise OSError('Failed to remove holders from %s', device)
        zero_device(device)
        cmd.append(device)

    if spares:
        cmd.append(f"--spare-devices={len(spares)}")
        for device in spares:
            zero_device(device)
            cmd.append(device)

    # Create the raid device
    udev.udevadm_settle()
    util.subp(["udevadm", "control", "--stop-exec-queue"])
    try:
        util.subp(cmd, capture=True)
    except util.ProcessExecutionError:
        # frequent issues by modules being missing (LP: #1519470) - add debug
        LOG.debug('mdadm_create failed - extra debug regarding md modules')
        (out, _err) = util.subp(["lsmod"], capture=True)
        if not _err:
            LOG.debug('modules loaded: \n%s' % out)
        raidmodpath = f'/lib/modules/{os.uname()[2]}/kernel/drivers/md'
        (out, _err) = util.subp(["find", raidmodpath],
                                rcs=[0, 1], capture=True)
        if out:
            LOG.debug('available md modules: \n%s' % out)
        else:
            LOG.debug('no available md modules found')

        for dev in devices + spares:
            h = get_holders(dev)
            LOG.debug('Device %s has holders: %s', dev, h)
        raise

    util.subp(["udevadm", "control", "--start-exec-queue"])
    udev.udevadm_settle(exists=md_devname)


def mdadm_examine(devpath, export=MDADM_USE_EXPORT):
    ''' exectute mdadm --examine, and optionally
        append --export.
        Parse and return dict of key=val from output'''
    assert_valid_devpath(devpath)

    cmd = ["mdadm", "--examine"]
    if export:
        cmd.extend(["--export"])

    cmd.extend([devpath])
    try:
        (out, _err) = util.subp(cmd, capture=True)
    except util.ProcessExecutionError:
        LOG.debug(f'not a valid md member device: {devpath}')
        return {}

    return __mdadm_export_to_dict(out) if export else __mdadm_detail_to_dict(out)


def set_sync_action(devpath, action=None, retries=None):
    assert_valid_devpath(devpath)
    if not action:
        return

    if not retries:
        retries = [0.2] * 60

    sync_action = md_sysfs_attr_path(devpath, 'sync_action')
    if not os.path.exists(sync_action):
        # arrays without sync_action can't set values
        return

    LOG.info("mdadm set sync_action=%s on array %s", action, devpath)
    for (attempt, wait) in enumerate(retries):
        try:
            LOG.debug('mdadm: set sync_action %s attempt %s',
                      devpath, attempt)
            val = md_sysfs_attr(devpath, 'sync_action').strip()
            LOG.debug('sync_action = "%s" ? "%s"', val, action)
            if val != action:
                LOG.debug("mdadm: setting array sync_action=%s", action)
                try:
                    util.write_file(sync_action, content=action)
                except (IOError, OSError) as e:
                    LOG.debug("mdadm: (non-fatal) write to %s failed %s",
                              sync_action, e)
            else:
                LOG.debug("mdadm: set array sync_action=%s SUCCESS", action)
                return

        except util.ProcessExecutionError:
            LOG.debug(
                "mdadm: set sync_action failed, retrying in %s seconds", wait)
            time.sleep(wait)


def mdadm_stop(devpath, retries=None):
    assert_valid_devpath(devpath)
    if not retries:
        retries = [0.2] * 60

    sync_action = md_sysfs_attr_path(devpath, 'sync_action')
    sync_max = md_sysfs_attr_path(devpath, 'sync_max')
    sync_min = md_sysfs_attr_path(devpath, 'sync_min')

    LOG.info(f"mdadm stopping: {devpath}")
    for (attempt, wait) in enumerate(retries):
        try:
            LOG.debug('mdadm: stop on %s attempt %s', devpath, attempt)
            # An array in 'resync' state may not be stoppable, attempt to
            # cancel an ongoing resync
            val = md_sysfs_attr(devpath, 'sync_action')
            LOG.debug('%s/sync_max = %s', sync_action, val)
            if val != "idle":
                LOG.debug("mdadm: setting array sync_action=idle")
                try:
                    util.write_file(sync_action, content="idle")
                except (IOError, OSError) as e:
                    LOG.debug("mdadm: (non-fatal) write to %s failed %s",
                              sync_action, e)

            # Setting the sync_{max,min} may can help prevent the array from
            # changing back to 'resync' which may prevent the array from being
            # stopped
            val = md_sysfs_attr(devpath, 'sync_max')
            LOG.debug('%s/sync_max = %s', sync_max, val)
            if val != "0":
                LOG.debug("mdadm: setting array sync_{min,max}=0")
                try:
                    for sync_file in [sync_max, sync_min]:
                        util.write_file(sync_file, content="0")
                except (IOError, OSError) as e:
                    LOG.debug('mdadm: (non-fatal) write to %s failed %s',
                              sync_file, e)

            # one wonders why this command doesn't do any of the above itself?
            out, err = util.subp(["mdadm", "--manage", "--stop", devpath],
                                 capture=True)
            LOG.debug("mdadm stop command output:\n%s\n%s", out, err)
            LOG.info("mdadm: successfully stopped %s after %s attempt(s)",
                     devpath, attempt+1)
            return

        except util.ProcessExecutionError:
            LOG.warning("mdadm stop failed, retrying ")
            if os.path.isfile('/proc/mdstat'):
                LOG.critical("/proc/mdstat:\n%s",
                             util.load_file('/proc/mdstat'))
            LOG.debug("mdadm: stop failed, retrying in %s seconds", wait)
            time.sleep(wait)
    raise OSError('Failed to stop mdadm device %s', devpath)


def mdadm_remove(devpath):
    assert_valid_devpath(devpath)

    LOG.info(f"mdadm removing: {devpath}")
    out, err = util.subp(["mdadm", "--remove", devpath],
                         rcs=[0], capture=True)
    LOG.debug("mdadm remove:\n%s\n%s", out, err)


def fail_device(mddev, arraydev):
    assert_valid_devpath(mddev)

    LOG.info("mdadm mark faulty: %s in array %s", arraydev, mddev)
    out, err = util.subp(["mdadm", "--fail", mddev, arraydev],
                         rcs=[0], capture=True)
    LOG.debug("mdadm mark faulty:\n%s\n%s", out, err)


def remove_device(mddev, arraydev):
    assert_valid_devpath(mddev)

    LOG.info("mdadm remove %s from array %s", arraydev, mddev)
    out, err = util.subp(["mdadm", "--remove", mddev, arraydev],
                         rcs=[0], capture=True)
    LOG.debug("mdadm remove:\n%s\n%s", out, err)


def zero_device(devpath, force=False):
    """ Wipe mdadm member device at data offset.

    For mdadm devices with metadata version 1.1 or newer location
    of the data offset is provided.  This value is used to determine
    the location to start wiping data to clear data.

    If metadata version is older then fallback to wiping 1MB at
    start and end of device; metadata was at end of device.
    """
    assert_valid_devpath(devpath)
    metadata = mdadm_examine(devpath, export=False)
    if not metadata and not force:
        LOG.debug('%s not mdadm member, force=False so skiping zeroing',
                  devpath)
        return
    LOG.debug('mdadm.examine metadata:\n%s', util.json_dumps(metadata))
    version = metadata.get('version')

    offsets = []
    # wipe at start, end of device for metadata older than 1.1
    if version and version in ["1.1", "1.2"]:
        LOG.debug('mdadm %s has metadata version=%s, extracting offsets',
                  devpath, version)
        for field in ['super_offset', 'data_offset']:
            offset, unit = metadata[field].split()
            if unit == "sectors":
                offsets.append(int(offset) * 512)
            else:
                LOG.warning('Unexpected offset unit: %s', unit)

    if not offsets:
        offsets = [0, -(1024 * 1024)]

    LOG.info('mdadm: wiping md member %s @ offsets %s', devpath, offsets)
    zero_file_at_offsets(devpath, offsets, buflen=1024,
                         count=1024, strict=True)
    LOG.info(f'mdadm: successfully wiped {devpath}')


def mdadm_query_detail(md_devname, export=MDADM_USE_EXPORT, rawoutput=False):
    valid_mdname(md_devname)

    cmd = ["mdadm", "--query", "--detail"]
    if export:
        cmd.extend(["--export"])
    cmd.extend([md_devname])
    (out, err) = util.subp(cmd, capture=True)
    if rawoutput:
        return (out, err)

    return __mdadm_export_to_dict(out) if export else __mdadm_detail_to_dict(out)


def mdadm_detail_scan():
    (out, _err) = util.subp(["mdadm", "--detail", "--scan"], capture=True)
    if not _err:
        return out


def mdadm_run(md_device):
    return util.subp(["mdadm", "--run", md_device], capture=True)


def md_present(mdname):
    """Check if mdname is present in /proc/mdstat"""
    if not mdname:
        raise ValueError('md_present requires a valid md name')

    try:
        mdstat = util.load_file('/proc/mdstat')
    except IOError as e:
        if not util.is_file_not_found_exc(e):
            raise e

        LOG.warning('Failed to read /proc/mdstat; '
                    'md modules might not be loaded')
        return False
    md_kname = dev_short(mdname)
    return bool(
        present := [
            line
            for line in mdstat.splitlines()
            if line.split(":")[0].rstrip() == md_kname
        ]
    )


# ------------------------------ #
def valid_mdname(md_devname):
    assert_valid_devpath(md_devname)

    if not is_valid_device(md_devname):
        raise ValueError(f'Specified md device does not exist: {md_devname}')
    return True


def valid_devpath(devpath):
    return devpath.startswith('/dev') if devpath else False


def assert_valid_devpath(devpath):
    if not valid_devpath(devpath):
        raise ValueError("Invalid devpath: '%s'" % devpath)


def md_sysfs_attr_path(md_devname, attrname):
    """ Return the path to a md device attribute under the 'md' dir """
    # build /sys/class/block/<md_short>/md
    sysmd = sys_block_path(md_devname, "md")

    # append attrname
    return os.path.join(sysmd, attrname)


def md_sysfs_attr(md_devname, attrname, default=''):
    """ Return the attribute str of an md device found under the 'md' dir """
    attrdata = default
    if not valid_mdname(md_devname):
        raise ValueError(f'Invalid md devicename: [{md_devname}]')

    sysfs_attr_path = md_sysfs_attr_path(md_devname, attrname)
    if os.path.isfile(sysfs_attr_path):
        attrdata = util.load_file(sysfs_attr_path).strip()

    return attrdata


def md_raidlevel_short(raidlevel):
    if isinstance(raidlevel, int) or \
       raidlevel in ['linear', 'stripe', 'container']:
        return raidlevel

    return int(raidlevel.replace('raid', ''))


def md_minimum_devices(raidlevel):
    ''' return the minimum number of devices for a given raid level '''
    rl = md_raidlevel_short(raidlevel)
    if rl in [0, 1, 'linear', 'stripe', 'container']:
        return 2
    if rl in [5]:
        return 3
    return 4 if rl in [6, 10] else -1


def __md_check_array_state(md_devname, mode='READWRITE'):
    modes = {
        'READWRITE': READWRITE_RAID_STATES,
        'READONLY': READONLY_RAID_STATES,
        'ERROR': ERROR_RAID_STATES,
    }
    if mode not in modes:
        raise ValueError(f'Invalid Array State mode: {mode}')

    array_state = md_sysfs_attr(md_devname, 'array_state')
    return array_state in modes[mode]


def md_check_array_state_rw(md_devname):
    return __md_check_array_state(md_devname, mode='READWRITE')


def md_check_array_state_ro(md_devname):
    return __md_check_array_state(md_devname, mode='READONLY')


def md_check_array_state_error(md_devname):
    return __md_check_array_state(md_devname, mode='ERROR')


def __mdadm_export_to_dict(output):
    ''' convert Key=Value text output into dictionary '''
    return dict(tok.split('=', 1) for tok in shlex.split(output))


def __mdadm_detail_to_dict(input):
    ''' Convert mdadm --detail/--export output to dictionary

    /dev/vde:
              Magic : a92b4efc
            Version : 1.2
        Feature Map : 0x0
         Array UUID : 93a73e10:427f280b:b7076c02:204b8f7a
               Name : wily-foobar:0  (local to host wily-foobar)
      Creation Time : Sat Dec 12 16:06:05 2015
         Raid Level : raid1
       Raid Devices : 2

     Avail Dev Size : 20955136 (9.99 GiB 10.73 GB)
      Used Dev Size : 20955136 (9.99 GiB 10.73 GB)
         Array Size : 10477568 (9.99 GiB 10.73 GB)
        Data Offset : 16384 sectors
       Super Offset : 8 sectors
       Unused Space : before=16296 sectors, after=0 sectors
              State : clean
        Device UUID : 8fcd62e6:991acc6e:6cb71ee3:7c956919

        Update Time : Sat Dec 12 16:09:09 2015
      Bad Block Log : 512 entries available at offset 72 sectors
           Checksum : 65b57c2e - correct
             Events : 17


       Device Role : spare
       Array State : AA ('A' == active, '.' == missing, 'R' == replacing)
    '''
    data = {}

    if device := input.splitlines()[0][:-1]:
        data['device'] = device
    else:
        raise ValueError('Failed to determine device from input:\n%s', input)

    # start after the first newline
    remainder = input[input.find('\n')+1:]

    # keep only the first section (imsm container)
    arraysection = remainder.find('\n[')
    if arraysection != -1:
        remainder = remainder[:arraysection]

    #  FIXME: probably could do a better regex to match the LHS which
    #         has one, two or three words
    rem = r'(\w+|\w+\ \w+|\w+\ \w+\ \w+)\ \:\ ([a-zA-Z0-9\-\.,: \(\)=\']+)'
    for f in re.findall(rem, remainder, re.MULTILINE):
        key = f[0].replace(' ', '_').lower()
        val = f[1]
        if key in data:
            raise ValueError(f'Duplicate key in mdadm regex parsing: {key}')
        data[key] = val

    return data


def md_device_key_role(devname):
    if not devname:
        raise ValueError('Missing parameter devname')
    return f'MD_DEVICE_{dev_short(devname)}_ROLE'


def md_device_key_dev(devname):
    if not devname:
        raise ValueError('Missing parameter devname')
    return f'MD_DEVICE_{dev_short(devname)}_DEV'


def md_read_run_mdadm_map():
    '''
        md1 1.2 59beb40f:4c202f67:088e702b:efdf577a /dev/md1
        md0 0.90 077e6a9e:edf92012:e2a6e712:b193f786 /dev/md0

        return
        # md_shortname = (metaversion, md_uuid, md_devpath)
        data = {
            'md1': (1.2, 59beb40f:4c202f67:088e702b:efdf577a, /dev/md1)
            'md0': (0.90, 077e6a9e:edf92012:e2a6e712:b193f786, /dev/md0)
    '''

    mdadm_map = {}
    run_mdadm_map = '/run/mdadm/map'
    if os.path.exists(run_mdadm_map):
        with open(run_mdadm_map, 'r') as fp:
            data = fp.read().strip()
        for entry in data.split('\n'):
            (key, meta, md_uuid, dev) = entry.split()
            mdadm_map[key] = (meta, md_uuid, dev)

    return mdadm_map


def md_check_array_uuid(md_devname, md_uuid):
    valid_mdname(md_devname)

    # confirm we have /dev/{mdname} by following the udev symlink
    mduuid_path = f'/dev/disk/by-id/md-uuid-{md_uuid}'
    mdlink_devname = dev_path(os.path.realpath(mduuid_path))
    if md_devname != mdlink_devname:
        err = (
            'Mismatch between devname and md-uuid symlink: '
            + f'{mduuid_path} -> {mdlink_devname} != {md_devname}'
        )

        raise ValueError(err)


def md_get_uuid(md_devname):
    valid_mdname(md_devname)

    md_query = mdadm_query_detail(md_devname)
    return md_query.get('MD_UUID', None)


def _compare_devlist(expected, found):
    LOG.debug(f'comparing device lists: expected: {expected} found: {found}')
    expected = set(expected)
    found = set(found)
    if expected != found:
        missing = expected.difference(found)
        extra = found.difference(expected)
        raise ValueError(
            f"RAID array device list does not match. Missing: {missing} Extra: {extra}"
        )


def md_check_raidlevel(md_devname, detail, raidlevel):
    # Validate raidlevel against what curtin supports configuring
    if raidlevel not in VALID_RAID_LEVELS:
        err = (f'Invalid raidlevel: {raidlevel}' + ' Must be one of: ') + str(
            VALID_RAID_LEVELS
        )

        raise ValueError(err)
    # normalize raidlevel to the values mdadm prints.
    if isinstance(raidlevel, int) or len(raidlevel) <= 2:
        raidlevel = f'raid{str(raidlevel)}'
    elif raidlevel == 'stripe':
        raidlevel = 'raid0'
    elif raidlevel == 'mirror':
        raidlevel = 'raid1'
    actual_level = detail.get("MD_LEVEL")
    if actual_level != raidlevel:
        raise ValueError(
            "raid device %s should have level %r but has level %r" % (
                md_devname, raidlevel, actual_level))


def md_block_until_in_sync(md_devname):
    '''
    sync_completed
    This shows the number of sectors that have been completed of
    whatever the current sync_action is, followed by the number of
    sectors in total that could need to be processed.  The two
    numbers are separated by a '/'  thus effectively showing one
    value, a fraction of the process that is complete.
    A 'select' on this attribute will return when resync completes,
    when it reaches the current sync_max (below) and possibly at
    other times.
    '''
    # FIXME: use selectors to block on: /sys/class/block/mdX/md/sync_completed
    pass


def md_check_array_state(md_devname):
    # check array state

    writable = md_check_array_state_rw(md_devname)
    # Raid 0 arrays do not have degraded or sync_action sysfs
    # attributes.
    degraded = md_sysfs_attr(md_devname, 'degraded', None)
    sync_action = md_sysfs_attr(md_devname, 'sync_action', None)

    if not writable:
        raise ValueError(f'Array not in writable state: {md_devname}')
    if degraded is not None and degraded != "0":
        raise ValueError(f'Array in degraded state: {md_devname}')
    if degraded is not None and sync_action not in ("idle", "resync"):
        raise ValueError(f'Array is {sync_action}, not idle: {md_devname}')


def md_check_uuid(md_devname):
    if md_uuid := md_get_uuid(md_devname):
        md_check_array_uuid(md_devname, md_uuid)
    else:
        raise ValueError(f'Failed to get md UUID from device: {md_devname}')


def md_check_devices(md_devname, devices):
    if not devices or len(devices) == 0:
        raise ValueError('Cannot verify raid array with empty device list')

    # collect and compare raid devices based on md name versus
    # expected device list.
    #
    # NB: In some cases, a device might report as a spare until
    #     md has finished syncing it into the array.  Currently
    #     we fail the check since the specified raid device is not
    #     yet in its proper role.  Callers can check mdadm_sync_action
    #     state to see if the array is currently recovering, which would
    #     explain the failure.  Also  mdadm_degraded will indicate if the
    #     raid is currently degraded or not, which would also explain the
    #     failure.
    md_raid_devices = md_get_devices_list(md_devname)
    LOG.debug(f'md_check_devices: md_raid_devs: {str(md_raid_devices)}')
    _compare_devlist(devices, md_raid_devices)


def md_check_spares(md_devname, spares):
    # collect and compare spare devices based on md name versus
    # expected device list.
    md_raid_spares = md_get_spares_list(md_devname)
    _compare_devlist(spares, md_raid_spares)


def md_check_array_membership(md_devname, devices):
    # validate that all devices are members of the correct array
    md_uuid = md_get_uuid(md_devname)
    for device in devices:
        dev_examine = mdadm_examine(device, export=True)
        if 'MD_UUID' not in dev_examine:
            raise ValueError(f'Device is not part of an array: {device}')
        dev_uuid = dev_examine['MD_UUID']
        if dev_uuid != md_uuid:
            err = f"Device {device} is not part of {md_devname} array. "
            err += f"MD_UUID mismatch: device:{dev_uuid} != array:{md_uuid}"
            raise ValueError(err)


def md_check(md_devname, raidlevel, devices, spares, container):
    ''' Check passed in variables from storage configuration against
        the system we're running upon.
    '''
    LOG.debug(
        (
            'RAID validation: '
            + f'name={md_devname} raidlevel={raidlevel} devices={devices} spares={spares} container={container}'
        )
    )

    assert_valid_devpath(md_devname)

    detail = mdadm_query_detail(md_devname)

    if raidlevel != "container":
        md_check_array_state(md_devname)
    md_check_raidlevel(md_devname, detail, raidlevel)
    md_check_uuid(md_devname)
    if container is None:
        md_check_devices(md_devname, devices)
        md_check_spares(md_devname, spares)
        md_check_array_membership(md_devname, devices + spares)
    else:
        if 'MD_CONTAINER' not in detail:
            raise ValueError(f"{md_devname} is not in a container")
        actual_container = os.path.realpath(detail['MD_CONTAINER'])
        if actual_container != container:
            raise ValueError("%s is in container %r, not %r" % (
                md_devname, actual_container, container))

    LOG.debug(f'RAID array OK: {md_devname}')


def md_is_in_container(md_devname):
    return 'MD_CONTAINER' in mdadm_query_detail(md_devname)

# vi: ts=4 expandtab syntax=python
