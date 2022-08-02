# This file is part of curtin. See LICENSE file for copyright and license info.

from typing import Optional

import attr

from curtin import (block, util)
from curtin.commands.block_meta import (
    disk_handler as disk_handler_v1,
    get_path_to_storage_volume,
    make_dname,
    partition_handler as partition_handler_v1,
    partition_verify_sfdisk,
    )
from curtin.log import LOG
from curtin.storage_config import (
    GPT_GUID_TO_CURTIN_MAP,
    )
from curtin.udev import udevadm_settle


@attr.s(auto_attribs=True)
class PartTableEntry:
    number: int
    start: int
    size: int
    type: str
    uuid: Optional[str]
    bootable: bool = False

    def render(self):
        r = f'{self.number}: '
        for a in 'start', 'size', 'type', 'uuid':
            v = getattr(self, a)
            if v is not None:
                r += f' {a}={v}'
        if self.bootable:
            r += ' bootable'
        return r


ONE_MIB_BYTES = 1 << 20


def align_up(size, block_size):
    return (size + block_size - 1) & ~(block_size - 1)


def align_down(size, block_size):
    return size & ~(block_size - 1)


FLAG_TO_GUID = {
    flag: guid for (guid, (flag, typecode)) in GPT_GUID_TO_CURTIN_MAP.items()
    }
FLAG_TO_MBR_TYPE = {
    flag: typecode[:2].upper() for (guid, (flag, typecode))
    in GPT_GUID_TO_CURTIN_MAP.items()
    }
FLAG_TO_MBR_TYPE['extended'] = '05'


class SFDiskPartTable:

    label = None

    def __init__(self, sector_bytes):
        self.entries = []
        self._sector_bytes = sector_bytes
        if ONE_MIB_BYTES % sector_bytes != 0:
            raise Exception(
                f"sector_bytes {sector_bytes} does not divide 1MiB, cannot "
                "continue!")
        self.one_mib_sectors = ONE_MIB_BYTES // sector_bytes

    def bytes2sectors(self, amount):
        return int(util.human2bytes(amount)) // self._sector_bytes

    def sectors2bytes(self, amount):
        return amount * self._sector_bytes

    def render(self):
        r = [f'label: {self.label}', ''] + [e.render() for e in self.entries]
        return '\n'.join(r)

    def apply(self, device):
        sfdisk_script = self.render()
        LOG.debug("sfdisk input:\n---\n%s\n---\n", sfdisk_script)
        util.subp(
            ['sfdisk', '--no-tell-kernel', '--no-reread', device],
            data=sfdisk_script.encode('ascii'))
        util.subp(['partprobe', device])
        # sfdisk and partprobe (as invoked here) use ioctls to inform the
        # kernel that the partition table has changed so it can add and remove
        # device nodes for the partitions as needed. Unfortunately this is
        # asynchronous: we can return before the nodes are present in /dev (or
        # /sys for that matter). Calling "udevadm settle" is slightly
        # incoherent as udev has nothing to do with creating these nodes, but
        # at the same time, udev won't finish processing the events triggered
        # by the sfdisk until after the nodes for the partitions have been
        # updated by the kernel.
        udevadm_settle()


class GPTPartTable(SFDiskPartTable):

    label = 'gpt'

    def add(self, action):
        number = action.get('number', len(self.entries) + 1)
        if 'offset' in action:
            start = self.bytes2sectors(action['offset'])
        elif self.entries:
            prev = self.entries[-1]
            start = align_up(prev.start + prev.size, self.one_mib_sectors)
        else:
            start = self.one_mib_sectors
        size = self.bytes2sectors(action['size'])
        uuid = action.get('uuid')
        type = FLAG_TO_GUID.get(action.get('flag'))
        entry = PartTableEntry(number, start, size, type, uuid)
        self.entries.append(entry)
        return entry


class DOSPartTable(SFDiskPartTable):

    label = 'dos'
    _extended = None

    def add(self, action):
        flag = action.get('flag', None)
        start = action.get('offset', None)
        if start is not None:
            start = self.bytes2sectors(start)
        if flag == 'logical':
            if self._extended is None:
                raise Exception("logical partition without extended partition")
            prev = None
            for entry in reversed(self.entries):
                if entry.number > 4:
                    prev = entry
                    break
            # The number of an logical partition cannot be specified (so the
            # 'number' from the action is completely ignored here) as the
            # partitions are numbered by the order they are found in the linked
            # list of logical partitions. sfdisk just cares that we put a
            # number > 4 here, in fact we could "number" every logical
            # partition as "5" but it's not hard to put the number that the
            # partition will end up getting into the sfdisk input.
            if prev is None:
                number = 5
                if start is None:
                    start = align_up(
                        self._extended.start + self.one_mib_sectors,
                        self.one_mib_sectors)
            else:
                number = prev.number + 1
                if start is None:
                    start = align_up(
                        prev.start + prev.size + self.one_mib_sectors,
                        self.one_mib_sectors)
        else:
            number = action.get('number', len(self.entries) + 1)
            if number > 4:
                raise Exception(f"primary partition cannot have number {number}")
            if start is None:
                prev = None
                for entry in self.entries:
                    if entry.number <= 4:
                        prev = entry
                if prev is None:
                    start = self.one_mib_sectors
                else:
                    start = align_up(
                        prev.start + prev.size,
                        self.one_mib_sectors)
        size = self.bytes2sectors(action['size'])
        type = FLAG_TO_MBR_TYPE.get(flag)
        bootable = True if flag == 'boot' else None
        entry = PartTableEntry(
            number, start, size, type, uuid=None, bootable=bootable)
        if flag == 'extended':
            self._extended = entry
        self.entries.append(entry)
        return entry


def _find_part_info(sfdisk_info, offset):
    for part in sfdisk_info['partitions']:
        if part['start'] == offset:
            return part
    raise Exception(
        "could not find existing partition by offset")


def _wipe_for_action(action):
    # If a wipe action is specified, do that.
    if 'wipe' in action:
        return action['wipe']
    # Existing partitions are left alone by default.
    if action.get('preserve', False):
        return None
    # New partitions are wiped by default apart from extended partitions, where
    # it would destroy the EBR.
    return None if action.get('flag') == 'extended' else 'superblock'


def disk_handler_v2(info, storage_config, handlers):
    disk_handler_v1(info, storage_config, handlers)

    part_actions = [
        action
        for action in storage_config.values()
        if action['type'] == 'partition' and action['device'] == info['id']
    ]


    table_cls = {
        'msdos': DOSPartTable,
        'gpt': GPTPartTable,
        }.get(info.get('ptable'))

    if table_cls is None:
        for action in part_actions:
            partition_handler_v1(action, storage_config, handlers)
        return

    disk = get_path_to_storage_volume(info.get('id'), storage_config)
    (sector_size, _) = block.get_blockdev_sector_size(disk)

    table = table_cls(sector_size)
    preserved_offsets = set()
    wipes = {}

    sfdisk_info = None
    for action in part_actions:
        entry = table.add(action)
        if action.get('preserve', False):
            if sfdisk_info is None:
                # Lazily computing sfdisk_info is slightly more efficient but
                # the real reason for doing this is that calling sfdisk_info on
                # a disk with no partition table logs messages that makes the
                # vmtest infrastructure unhappy.
                sfdisk_info = block.sfdisk_info(disk)
            part_info = _find_part_info(sfdisk_info, entry.start)
            partition_verify_sfdisk(action, sfdisk_info['label'], part_info)
            preserved_offsets.add(entry.start)
        wipe = wipes[entry.start] = _wipe_for_action(action)
        if wipe is not None:
            # We do a quick wipe of where any new partitions will be,
            # because if there is bcache or other metadata there, this
            # can cause the partition to be used by a storage
            # subsystem and preventing the exclusive open done by the
            # wipe_volume call below. See
            # https://bugs.launchpad.net/curtin/+bug/1718699 for all
            # the gory details.
            wipe_offset = table.sectors2bytes(entry.start)
            LOG.debug('Wiping 1M on %s at offset %s', disk, wipe_offset)
            block.zero_file_at_offsets(disk, [wipe_offset], exclusive=False)

    # Do a superblock wipe of any partitions that are being deleted.
    for kname, nr, offset, sz in block.sysfs_partition_data(disk):
        offset_sectors = table.bytes2sectors(offset)
        if offset_sectors not in preserved_offsets:
            block.wipe_volume(block.kname_to_path(kname), 'superblock')

    table.apply(disk)

    # Wipe the new partitions as needed.
    for kname, number, offset, size in block.sysfs_partition_data(disk):
        offset_sectors = table.bytes2sectors(offset)
        mode = wipes[offset_sectors]
        if mode is not None:
            block.wipe_volume(block.kname_to_path(kname), mode)

    # Make the names if needed
    if 'name' in info:
        for action in part_actions:
            if action.get('flag') != 'extended':
                make_dname(action['id'], storage_config)


def partition_handler_v2(info, storage_config, handlers):
    pass


# vi: ts=4 expandtab syntax=python
