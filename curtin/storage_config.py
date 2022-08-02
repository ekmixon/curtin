# This file is part of curtin. See LICENSE file for copyright and license info.
from collections import namedtuple, OrderedDict
import copy
import operator
import os
import re
import yaml

from curtin.log import LOG
from curtin.block import multipath, schemas
from curtin import config as curtin_config
from curtin import util

# map
# https://en.wikipedia.org/wiki/GUID_Partition_Table#Partition_type_GUIDs
# to
# curtin/commands/block_meta.py:partition_handler()sgdisk_flags/types
GPT_GUID_TO_CURTIN_MAP = {
    'C12A7328-F81F-11D2-BA4B-00A0C93EC93B': ('boot', 'EF00'),
    '21686148-6449-6E6F-744E-656564454649': ('bios_grub', 'EF02'),
    '933AC7E1-2EB4-4F13-B844-0E14E2AEF915': ('home', '8302'),
    '0FC63DAF-8483-4772-8E79-3D69D8477DE4': ('linux', '8300'),
    'E6D6D379-F507-44C2-A23C-238F2A3DF928': ('lvm', '8e00'),
    '024DEE41-33E7-11D3-9D69-0008C781F39F': ('mbr', ''),
    '9E1A2D38-C612-4316-AA26-8B49521E5A8B': ('prep', '4200'),
    'A19D880F-05FC-4D3B-A006-743F0F84911E': ('raid', 'fd00'),
    '0657FD6D-A4AB-43C4-84E5-0933C84B4F4F': ('swap', '8200'),
}

# MBR types
# https://www.win.tue.nl/~aeb/partitions/partition_types-2.html
# to
# curtin/commands/block_meta.py:partition_handler()sgdisk_flags/types
MBR_TYPE_TO_CURTIN_MAP = {
    '0XF': ('extended', 'f'),
    '0X5': ('extended', 'f'),
    '0X83': ('linux', '83'),
    '0X85': ('extended', 'f'),
    '0XC5': ('extended', 'f'),
}

MBR_BOOT_FLAG = '0x80'

PTABLE_TYPE_MAP = dict(GPT_GUID_TO_CURTIN_MAP, **MBR_TYPE_TO_CURTIN_MAP)

StorageConfig = namedtuple('StorageConfig', ('type', 'schema'))
STORAGE_CONFIG_TYPES = {
    'bcache': StorageConfig(type='bcache', schema=schemas.BCACHE),
    'dasd': StorageConfig(type='dasd', schema=schemas.DASD),
    'disk': StorageConfig(type='disk', schema=schemas.DISK),
    'dm_crypt': StorageConfig(type='dm_crypt', schema=schemas.DM_CRYPT),
    'format': StorageConfig(type='format', schema=schemas.FORMAT),
    'lvm_partition': StorageConfig(type='lvm_partition',
                                   schema=schemas.LVM_PARTITION),
    'lvm_volgroup': StorageConfig(type='lvm_volgroup',
                                  schema=schemas.LVM_VOLGROUP),
    'mount': StorageConfig(type='mount', schema=schemas.MOUNT),
    'partition': StorageConfig(type='partition', schema=schemas.PARTITION),
    'raid': StorageConfig(type='raid', schema=schemas.RAID),
    'zfs': StorageConfig(type='zfs', schema=schemas.ZFS),
    'zpool': StorageConfig(type='zpool', schema=schemas.ZPOOL),
}


def get_storage_types():
    return copy.deepcopy(STORAGE_CONFIG_TYPES)


def get_storage_type_schemas():
    return [stype.schema for stype in sorted(get_storage_types().values())]


STORAGE_CONFIG_SCHEMA = {
    '$schema': 'http://json-schema.org/draft-04/schema#',
    'name': 'ASTORAGECONFIG',
    'title': 'curtin storage configuration for an installation.',
    'description': (
        'Declaritive syntax for specifying storage device configuration.'),
    'required': ['version', 'config'],
    'definitions': schemas.definitions,
    'properties': {
        'version': {'type': 'integer', 'enum': [1, 2]},
        'config': {
            'type': 'array',
            'items': {
                'oneOf': get_storage_type_schemas(),
            },
            'additionalItems': False,
        },
    },
    'additionalProperties': False,
}


def load_and_validate(config_path):
    """Load and validate storage config file."""
    config = curtin_config.load_config(config_path)
    if 'storage' not in config:
        LOG.info('Skipping %s, missing "storage" key' % config_path)
        return

    return validate_config(config.get('storage'), sourcefile=config_path)


def validate_config(config, sourcefile=None):
    """Validate storage config object."""
    if not sourcefile:
        sourcefile = ''
    try:
        import jsonschema
        jsonschema.validate(config, STORAGE_CONFIG_SCHEMA)
    except ImportError:
        LOG.error('Cannot validate storage config, missing jsonschema')
        raise
    except jsonschema.exceptions.ValidationError as e:
        if isinstance(e.instance, int):
            msg = 'Unexpected value (%s) for property "%s"' % (e.path[0],
                                                               e.instance)
            raise ValueError(msg)
        if 'type' not in e.instance:
            msg = f"{e.message} in {e.instance}"
            raise ValueError(msg)

        instance_type = e.instance['type']
        if stype := get_storage_types().get(instance_type):
            try:
                jsonschema.validate(e.instance, stype.schema)
            except jsonschema.exceptions.ValidationError as f:
                msg = "%s in %s\n%s" % (f.message, sourcefile,
                                        util.json_dumps(e.instance))
                raise(ValueError(msg))
        else:
            msg = f"Unknown storage type: {instance_type} in {e.instance}"
            raise ValueError(msg)


# FIXME: move this map to each types schema and extract these
# values from each type's schema.
def _stype_to_deps(stype):
    """ Return a set of storage_config type keys for storage_config type.

        The strings returned in a dep set indicate which fields reference
        other storage_config elements that require a lookup.

        config:
         - type: disk
           id: sda
           path: /dev/sda
           ptable: gpt
         - type: partition
           id: sda1
           device: sda
    """

    depends_keys = {
        'bcache': {'backing_device', 'cache_device'},
        'dasd': set(),
        'disk': set(),
        'dm_crypt': {'volume'},
        'format': {'volume'},
        'lvm_partition': {'volgroup'},
        'lvm_volgroup': {'devices'},
        'mount': {'device'},
        'partition': {'device'},
        'raid': {'devices', 'spare_devices', 'container'},
        'zfs': {'pool'},
        'zpool': {'vdevs'},
    }
    return depends_keys[stype]


def _stype_to_order_key(stype):
    default_sort = {'id'}
    order_key = {
        'bcache': {'name'},
        'dasd': default_sort,
        'disk': default_sort,
        'dm_crypt': default_sort,
        'format': default_sort,
        'lvm_partition': {'name'},
        'lvm_volgroup': {'name'},
        'mount': {'path'},
        'partition': {'number'},
        'raid': default_sort,
        'zfs': {'volume'},
        'zpool': default_sort,
    }
    if stype not in order_key:
        raise ValueError(f'Unknown storage type: {stype}')

    return order_key.get(stype)


# Document what each storage type can be composed from.
def _validate_dep_type(source_id, dep_key, dep_id, sconfig):
    '''check if dependency type is in the list of allowed by source'''

    # FIXME: this should come from curtin.block.schemas.*
    depends = {
        'bcache': {'bcache', 'disk', 'dm_crypt', 'lvm_partition',
                   'partition', 'raid'},
        'dasd': {},
        'disk': {'dasd'},
        'dm_crypt': {'bcache', 'disk', 'dm_crypt', 'lvm_partition',
                     'partition', 'raid'},
        'format': {'bcache', 'disk', 'dm_crypt', 'lvm_partition',
                   'partition', 'raid'},
        'lvm_partition': {'lvm_volgroup'},
        'lvm_volgroup': {'bcache', 'disk', 'dm_crypt', 'partition', 'raid'},
        'mount': {'format'},
        'partition': {'bcache', 'disk', 'raid', 'partition'},
        'raid': {'bcache', 'disk', 'dm_crypt', 'lvm_partition',
                 'partition', 'raid'},
        'zfs': {'zpool'},
        'zpool': {'disk', 'partition'},
    }
    if source_id not in sconfig:
        raise ValueError(f'Invalid source_id ({source_id}) not in storage config')
    if dep_id not in sconfig:
        raise ValueError(f'Invalid dep_id ({dep_id}) not in storage config')

    source_type = sconfig[source_id]['type']
    dep_type = sconfig[dep_id]['type']

    if source_type not in depends:
        raise ValueError(f'Invalid source_type: {source_type}')
    if dep_type not in depends:
        raise ValueError(f'Invalid type in depedency: {dep_type}')

    source_deps = depends[source_type]
    result = dep_type in source_deps
    LOG.debug('Validate: %s:SourceType:%s -> (DepId:%s DepType:%s) in '
              'SourceDeps:%s ? result=%s' % (source_id, source_type,
                                             dep_id, dep_type,
                                             source_deps, result))
    if not result:
        # Partition(sda1).device -> Partition(sda3)
        s_str = f'{source_type.capitalize()}(id={source_id}).{dep_key}'
        d_str = f'{dep_type.capitalize()}(id={dep_id})'
        dep_chain = f"{s_str} cannot depend upon on {d_str}"
        raise ValueError(dep_chain)

    return result


def find_item_dependencies(item_id, config, validate=True):
    """ Walk a storage config collecting any dependent device ids."""

    if not config or not isinstance(config, OrderedDict):
        raise ValueError('Invalid config. Must be non-empty OrderedDict')

    item_cfg = config.get(item_id)
    if not item_cfg:
        return None

    def _find_same_dep(dep_key, dep_value, config):
        return [item_id for item_id, item_cfg in config.items()
                if item_cfg.get(dep_key) == dep_value]

    deps = []
    item_type = item_cfg.get('type')
    item_order = _stype_to_order_key(item_type)
    for dep_key in _stype_to_deps(item_type):
        if dep_key in item_cfg:
            dep_value = item_cfg[dep_key]
            if not isinstance(dep_value, list):
                dep_value = [dep_value]
            deps.extend(dep_value)
            for dep in dep_value:
                if validate:
                    _validate_dep_type(item_id, dep_key, dep, config)

                # find other items with the same dep_key, dep_value
                same_deps = _find_same_dep(dep_key, dep, config)
                sdeps_cfgs = [cfg for sdep, cfg in config.items()
                              if sdep in same_deps]
                sorted_deps = (
                    sorted(sdeps_cfgs,
                           key=operator.itemgetter(*list(item_order))))
                for sdep in sorted_deps:
                    deps.append(sdep['id'])

                # find lower level deps
                lower_deps = find_item_dependencies(dep, config)
                if lower_deps:
                    deps.extend(lower_deps)

    return deps


def get_config_tree(item, storage_config):
    '''Construct an OrderedDict which inserts all of the
       storage config dependencies required to construct
       the device specifed by item_id.

    '''
    sconfig = extract_storage_ordered_dict(storage_config)
    # Create the OrderedDict by inserting the top-most item
    # and then inserting the next dependency.
    item_deps = OrderedDict({item: sconfig[item]})
    for dep in find_item_dependencies(item, sconfig):
        item_deps[dep] = sconfig[dep]
    return item_deps


def merge_config_trees_to_list(config_trees):
    ''' Create a registry to track each tree by
        device_id, and capture the dependency level
        and config of each tree.

        From this registry we can return a list that
        is sorted from the least to most dependent
        configuration item.  This calculation ensures
        that composed devices are listed last.
    '''

    reg = {}
    # reg[sda] = {level=0, config={}}
    # reg[sdd] = {level=0, config={}}
    # reg[sde] = {level=0, config={}}
    # reg[sdf] = {level=0, config={}}
    # reg[md0] = {level=3, config={'devices': [sdd, sde, sdf]}}
    # reg[sda5] = {level=1, config={'device': sda}}
    # reg[bcache1_raid] =
    #    {level=5, config={'backing': ['md0'], 'cache': ['sda5']}}
    max_level = 0
    for tree in config_trees:
        top_item_id = list(tree.keys())[0]  # first insertion has the most deps
        level = len(tree.keys())
        if level > max_level:
            max_level = level
        item_cfg = tree[top_item_id]
        if top_item_id in reg:
            LOG.warning(f'Dropping Duplicate id: {top_item_id}')
            continue
        reg[top_item_id] = {'level': level, 'config': item_cfg}

    def sort_level(configs):
        sreg = {}
        for cfg in configs:
            if cfg['type'] in sreg:
                sreg[cfg['type']].append(cfg)
            else:
                sreg[cfg['type']] = [cfg]

        result = []
        for item_type in sorted(sreg.keys()):
            iorder = _stype_to_order_key(item_type)
            isorted = sorted(sreg[item_type],
                             key=operator.itemgetter(*list(iorder)))
            result.extend(isorted)

        return result

    # [entry for tag in tags]
    merged = []
    for lvl in range(max_level + 1):
        level_configs = []
        for item_id, entry in reg.items():
            if entry['level'] == lvl:
                level_configs.append(entry['config'])

        sconfigs = sort_level(level_configs)
        merged.extend(sconfigs)

    return merged


def config_tree_to_list(config_tree):
    """ ConfigTrees are OrderedDicts which insert dependent storage configs
        from leaf to root.  Reversing this insertion order creates a list
        of storage_configuration that is in the correct order for use by
        block_meta.
    """
    return [config_tree[item] for item in reversed(config_tree)]


def extract_storage_ordered_dict(config):
    storage_config = config.get('storage')
    if not storage_config:
        raise ValueError("no 'storage' entry in config")
    if scfg := storage_config.get('config'):
        # Since storage config will often have to be searched for a value by its
        # id, and this can become very inefficient as storage_config grows, a dict
        # will be generated with the id of each component of the storage_config as
        # its index and the component of storage_config as its value
        return OrderedDict((d["id"], d) for d in scfg)
    else:
        raise ValueError("invalid storage config data")


class ProbertParser(object):
    """ Base class for parsing probert storage configuration.

        This will hold common methods of the various storage type
        parsers.
    """
    # In subclasses 'probe_data_key' value will select a subset of
    # Probert probe_data if the value is present.  If the probe_data
    # is incomplete, we raise a ValuError. This selection  allows the
    # subclass to handle parsing one portion of the data and will be
    # accessed in the subclass via 'class_data' member.
    probe_data_key = None
    class_data = None

    def __init__(self, probe_data):
        if not probe_data or not isinstance(probe_data, dict):
            raise ValueError(f'Invalid probe_data: {probe_data}')

        self.probe_data = probe_data
        if self.probe_data_key is not None:
            if self.probe_data_key in probe_data:
                data = self.probe_data.get(self.probe_data_key) or {}
                self.class_data = data
            else:
                LOG.warning('probe_data missing %s data', self.probe_data_key)
                self.class_data = {}

        # We keep a reference to the blockdev_data on the superclass
        # as each specific parser has common needs to reference
        # this data separate from the BlockdevParser class.
        self.blockdev_data = self.probe_data.get('blockdev', {})
        if not self.blockdev_data:
            LOG.warning('probe_data missing valid "blockdev" data')

    def parse(self):
        raise NotImplementedError()

    def asdict(self, data):
        raise NotImplementedError()

    def lookup_devname(self, devname):
        """ Search 'blockdev' space for "devname".  The device
            name may not be a kernel name, so if not found in
            the dictionary keys, search under 'DEVLINKS' of each
            device and return the dictionary for the kernel.
        """
        if devname in self.blockdev_data:
            return devname

        for bd_key, bdata in self.blockdev_data.items():
            devlinks = bdata.get('DEVLINKS', '').split()
            if devname in devlinks:
                return bd_key

        return None

    def is_mpath_member(self, blockdev):
        return multipath.is_mpath_member(blockdev.get('DEVNAME', ''), blockdev)

    def is_mpath_device(self, blockdev):
        return multipath.is_mpath_device(blockdev.get('DEVNAME', ''), blockdev)

    def is_mpath_partition(self, blockdev):
        return multipath.is_mpath_partition(
            blockdev.get('DEVNAME', ''), blockdev)

    def blockdev_to_id(self, blockdev):
        """ Examine a blockdev dictionary and return a tuple of curtin
            storage type and name that can be used as a value for
            storage_config ids (opaque reference to other storage_config
            elements).
        """

        def is_dmcrypt(blockdev):
            return bool(blockdev.get('DM_UUID', '').startswith('CRYPT-LUKS'))

        devtype = blockdev.get('DEVTYPE', 'MISSING')
        devname = blockdev.get('DEVNAME', 'MISSING')
        name = os.path.basename(devname)
        if devname.startswith('/dev/dm-'):
            # device mapper names are composed deviecs, let's
            # look at udev data to see what it's really
            if 'DM_LV_NAME' in blockdev:
                devtype = 'lvm-partition'
                name = blockdev['DM_LV_NAME']
            elif self.is_mpath_device(blockdev):
                devtype = 'mpath-disk'
                name = blockdev['DM_NAME']
            elif self.is_mpath_partition(blockdev):
                devtype = 'mpath-partition'
                name = '{}-part{}'.format(
                    blockdev['DM_MPATH'], blockdev['DM_PART'])
            elif is_dmcrypt(blockdev):
                devtype = 'dmcrypt'
                name = blockdev['DM_NAME']
        elif devname.startswith('/dev/md'):
            devtype = 'raid'

        for key, val in {'name': name, 'devtype': devtype}.items():
            if not val or val == 'MISSING':
                msg = f'Failed to extract {key} data: {blockdev}'
                raise ValueError(msg)

        return f"{devtype}-{name}"

    def blockdev_byid_to_devname(self, link):
        """ Lookup blockdev by devlink and convert to storage_config id. """
        if bd_key := self.lookup_devname(link):
            return self.blockdev_to_id(self.blockdev_data[bd_key])
        return None


class BcacheParser(ProbertParser):

    probe_data_key = 'bcache'

    def __init__(self, probe_data):
        super(BcacheParser, self).__init__(probe_data)
        self.backing = self.class_data.get('backing', {})
        self.caching = self.class_data.get('caching', {})

    def parse(self):
        """parse probert 'bcache' data format.

           Collects storage config type: bcache for valid
           data and returns tuple of lists, configs, errors.
        """
        configs = []
        errors = []
        for dev_uuid, bdata in self.backing.items():
            if entry := self.asdict(dev_uuid, bdata):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)

    def asdict(self, backing_uuid, backing_data):
        """ process a specific bcache entry and return
            a curtin storage config dictionary. """

        def _sb_get(data, attr):
            return data.get('superblock', {}).get(attr)

        def _find_cache_device(backing_data, cache_data):
            cset_uuid = _sb_get(backing_data, 'cset.uuid')
            msg = ('Invalid "blockdev" value for cache device '
                   'uuid=%s' % cset_uuid)
            if not cset_uuid:
                LOG.warning(msg)
                return None

            for devuuid, config in cache_data.items():
                cache = _sb_get(config, 'cset.uuid')
                if cache == cset_uuid:
                    return config['blockdev']

            return None

        def _find_bcache_devname(uuid, backing_data, blockdev_data):
            by_uuid = f'/dev/bcache/by-uuid/{uuid}'
            label = _sb_get(backing_data, 'dev.label')
            for devname, data in blockdev_data.items():
                if not devname:
                    continue
                if devname.startswith('/dev/bcache'):
                    # DEVLINKS is a space separated list
                    devlinks = data.get('DEVLINKS', '').split()
                    if by_uuid in devlinks:
                        return devname
            if label:
                return label
            LOG.warning(f'Failed to find bcache {by_uuid} ')

        def _cache_mode(dev_data):
            # "1 [writeback]" -> "writeback"
            attr = _sb_get(dev_data, 'dev.data.cache_mode')
            return attr.split()[1][1:-1] if attr else None

        if not self.blockdev_data:
            return None

        backing_device = backing_data.get('blockdev')
        cache_device = _find_cache_device(backing_data, self.caching)
        cache_mode = _cache_mode(backing_data)
        bcache_name = os.path.basename(_find_bcache_devname(backing_uuid,
                                       backing_data, self.blockdev_data))
        bcache_entry = {
            'type': 'bcache',
            'id': f'disk-{bcache_name}',
            'name': bcache_name,
        }


        if cache_mode:
            bcache_entry['cache_mode'] = cache_mode
        if backing_device:
            bcache_entry['backing_device'] = self.blockdev_to_id(
                self.blockdev_data[backing_device])

        if cache_device:
            bcache_entry['cache_device'] = self.blockdev_to_id(
                self.blockdev_data[cache_device])

        return bcache_entry


class BlockdevParser(ProbertParser):

    probe_data_key = 'blockdev'

    def parse(self):
        """ parse probert 'blockdev' data format.

            returns tuple with list of blockdev entries converted to
            storage config and any validation errors.
        """
        configs = []
        errors = []

        for devname, data in self.blockdev_data.items():
            # skip composed devices here, except partitions and multipath
            if (
                data.get('DEVPATH', '').startswith('/devices/virtual/block')
                and not self.is_mpath_device(data)
                and not self.is_mpath_partition(data)
                and data.get('DEVTYPE', '') != "partition"
            ):
                continue
            # skip disks that are members of multipath devices
            if self.is_mpath_member(data):
                continue
            if entry := self.asdict(data):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)

    def valid_id(self, id_value):
        # reject wwn=0x0+
        if id_value.lower().startswith('0x'):
            try:
                return int(id_value, 16) > 0
            except ValueError:
                return True
        # accept non-empty (removing whitspace) strings
        return ''.join(id_value.split()) != ""

    def get_unique_ids(self, blockdev):
        """ extract preferred ID_* keys for www and serial values.

            In some cases, ID_ values have duplicate values, this
            method returns the preferred value for a specific
            blockdev attribute.
        """
        uniq = {}
        if self.is_mpath_device(blockdev):
            source_keys = {
                'wwn': ['DM_WWN'],
                'serial': ['DM_SERIAL'],  # only present with focal+
            }
        else:
            source_keys = {
                'wwn': ['ID_WWN_WITH_EXTENSION', 'ID_WWN'],
                'serial': ['ID_SERIAL', 'ID_SERIAL_SHORT'],
            }
        for skey, id_keys in source_keys.items():
            for id_key in id_keys:
                if (
                    id_key in blockdev
                    and skey not in uniq
                    and self.valid_id(blockdev[id_key])
                ):
                    uniq[skey] = blockdev[id_key]

        return uniq

    def partition_parent_devname(self, blockdev):
        """ Return the devname of a partition's parent.
        md0p1 -> /dev/md0
        vda1 -> /dev/vda
        nvme0n1p3 -> /dev/nvme0n1
        """
        if blockdev['DEVTYPE'] != "partition":
            raise ValueError('Invalid blockdev, DEVTYPE is not partition')

        if pdevpath := blockdev.get('DEVPATH'):
            return f'/dev/{os.path.basename(os.path.dirname(pdevpath))}'

    def asdict(self, blockdev_data):
        """ process blockdev_data and return a curtin
            storage config dictionary.  This method
            will return curtin storage types: disk, partition.
        """
        dev_type = blockdev_data['DEVTYPE']
        if self.is_mpath_partition(blockdev_data):
            dev_type = 'partition'

        # just disks and partitions
        if blockdev_data['DEVTYPE'] not in ["disk", "partition"]:
            return None

        # https://www.kernel.org/doc/Documentation/admin-guide/devices.txt
        # Ignore Floppy (block MAJOR=2), CDROM (block MAJOR=11)
        # XXX: Possible expansion on this in the future.
        if blockdev_data['MAJOR'] in ["11", "2"]:
            return None

        devname = blockdev_data.get('DEVNAME')
        entry = {
            'type': dev_type,
            'id': self.blockdev_to_id(blockdev_data),
        }
        if self.is_mpath_device(blockdev_data):
            entry['multipath'] = blockdev_data['DM_NAME']
        elif self.is_mpath_partition(blockdev_data):
            entry['multipath'] = blockdev_data['DM_MPATH']

        # default disks to gpt
        if entry['type'] == 'disk':
            uniq_ids = self.get_unique_ids(blockdev_data)
            # always include path, block_meta will prefer wwn/serial over path
            uniq_ids.update({'path': devname})
            # set wwn, serial, and path
            entry |= uniq_ids

            # disk entry for ECKD dasds needs device_id and check for vtoc
            # ptable
            dasd_config = self.probe_data.get('dasd', {}).get(devname)
            if dasd_config is not None:
                dasd_type = dasd_config.get('type', 'ECKD')
                if dasd_type == 'ECKD':
                    if device_id := (
                        blockdev_data.get('ID_PATH', '').replace('ccw-', '')
                    ):
                        entry['device_id'] = device_id

                if dasd_type in ['ECKD', 'virt']:
                    # if dasd has been formatted, attrs.size is non-zero
                    # formatted ECKD dasds have ptable type of 'vtoc'
                    dasd_size = blockdev_data.get('attrs', {}).get('size', "0")
                    if dasd_size != "0":
                        entry['ptable'] = 'vtoc'

            if 'ID_PART_TABLE_TYPE' in blockdev_data:
                ptype = blockdev_data['ID_PART_TABLE_TYPE']
                if ptype in schemas._ptables:
                    entry['ptable'] = ptype
                else:
                    entry['ptable'] = schemas._ptable_unsupported
            return entry

        if entry['type'] == 'partition':
            if devname:
                entry['path'] = devname
            attrs = blockdev_data['attrs']
            if self.is_mpath_partition(blockdev_data):
                entry['number'] = int(blockdev_data['DM_PART'])
                parent_devname = self.lookup_devname(
                    '/dev/mapper/' + blockdev_data['DM_MPATH'])
                if parent_devname is None:
                    raise ValueError(
                        f"Cannot find parent mpath device {blockdev_data['DM_MPATH']} for {devname}"
                    )

            else:
                entry['number'] = int(attrs['partition'])
                parent_devname = self.partition_parent_devname(blockdev_data)
            parent_blockdev = self.blockdev_data[parent_devname]
            if 'ID_PART_TABLE_TYPE' not in parent_blockdev:
                # Exclude the fake partition that the kernel creates
                # for an otherwise unformatted FBA dasd.
                dasds = self.probe_data.get('dasd', {})
                dasd_config = dasds.get(parent_devname, {})
                if dasd_config.get('type', 'ECKD') == 'FBA':
                    return None
            ptable = parent_blockdev.get('partitiontable')
            if ptable:
                part = next(
                    (
                        pentry
                        for pentry in ptable['partitions']
                        if self.lookup_devname(pentry['node']) == devname
                    ),
                    None,
                )

                if part is None:
                    raise RuntimeError(
                        "Couldn't find partition entry in table")
            else:
                part = attrs

            # sectors 512B sector units in both attrs and ptable
            offset_val = int(part['start']) * 512
            if offset_val > 0:
                entry['offset'] = offset_val

            # ptable size field is in sectors
            entry['size'] = int(part['size'])
            if ptable:
                entry['size'] *= 512

            ptype = blockdev_data.get('ID_PART_ENTRY_TYPE')
            flag_name, _flag_code = ptable_uuid_to_flag_entry(ptype)

            if ptable and ptable.get('label') == 'dos':
                # if the boot flag is set, use this as the flag, logical
                # flag is not required as we can determine logical via
                # partition number
                ptype_flag = blockdev_data.get('ID_PART_ENTRY_FLAGS')
                if ptype_flag in [MBR_BOOT_FLAG]:
                    flag_name = 'boot'
                elif entry['number'] > 4:
                    flag_name = 'logical'

            if flag_name:
                entry['flag'] = flag_name

            # determine parent blockdev and calculate the device id
            if parent_blockdev:
                if device_id := self.blockdev_to_id(parent_blockdev):
                    entry['device'] = device_id

        return entry


class FilesystemParser(ProbertParser):

    probe_data_key = 'filesystem'

    def parse(self):
        """parse probert 'filesystem' data format.

            returns tuple with list entries converted to
            storage config type:format and any validation errors.
        """
        configs = []
        errors = []
        for devname, data in self.class_data.items():
            blockdev_data = self.blockdev_data.get(devname)
            if not blockdev_data:
                err = ('No probe data found for blockdev '
                       '%s for fs: %s' % (devname, data))
                errors.append(err)
                continue

            if self.is_mpath_member(blockdev_data):
                continue

            # no floppy, no cdrom
            if blockdev_data['MAJOR'] in ["11", "2"]:
                continue

            volume_id = self.blockdev_to_id(blockdev_data)

            # don't capture non-filesystem usage
            # crypto is just a disguised filesystem
            if data['USAGE'] not in ("filesystem", "crypto"):
                continue

            entry = self.asdict(volume_id, data)
            if not entry:
                continue

            # allow types that we cannot create only if preserve == true
            if data.get('TYPE') not in schemas._fstypes:
                entry['preserve'] = True

            try:
                validate_config(entry)
            except ValueError as e:
                errors.append(e)
                continue
            configs.append(entry)
        return (configs, errors)

    def asdict(self, volume_id, fs_data):
        """ process fs_data and return a curtin storage config dict.
            This method will return curtin storage type: format.
        {
            'LABEL': xxxx,
            'TYPE': ext2,
            'UUID': .....,
        }
        """
        entry = {
            'id': f'format-{volume_id}',
            'type': 'format',
            'volume': volume_id,
            'fstype': fs_data.get('TYPE'),
        }

        if uuid := fs_data.get('UUID'):
            if valid_uuid := re.match(schemas._uuid_pattern, uuid):
                entry['uuid'] = uuid

        return entry


class LvmParser(ProbertParser):

    probe_data_key = 'lvm'

    def lvm_partition_asdict(self, lv_name, lv_config):
        return {
            'type': 'lvm_partition',
            'id': f"lvm-partition-{lv_config['name']}",
            'name': lv_config['name'],
            'size': lv_config['size'],
            'volgroup': f"lvm-volgroup-{lv_config['volgroup']}",
        }

    def lvm_volgroup_asdict(self, vg_name, vg_config):
        """ process volgroup probe structure into storage config dict."""
        blockdev_ids = []
        for pvol in vg_config.get('devices', []):
            pvol_bdev = self.lookup_devname(pvol)
            if blockdev_data := self.blockdev_data[pvol_bdev]:
                blockdev_ids.append(self.blockdev_to_id(blockdev_data))

        return {
            'type': 'lvm_volgroup',
            'id': f'lvm-volgroup-{vg_name}',
            'name': vg_name,
            'devices': sorted(blockdev_ids),
        }

    def parse(self):
        """parse probert 'lvm' data format.

            returns tuple with list entries converted to
            storage config type:lvm_partition, type:lvm_volgroup
            and any validation errors.
        """
        # exit early if lvm_data is empty
        if 'volume_groups' not in self.class_data:
            return ([], [])

        configs = []
        errors = []
        for vg_name, vg_config in self.class_data['volume_groups'].items():
            if entry := self.lvm_volgroup_asdict(vg_name, vg_config):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        for lv_name, lv_config in self.class_data['logical_volumes'].items():
            if entry := self.lvm_partition_asdict(lv_name, lv_config):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)

        return (configs, errors)


class DasdParser(ProbertParser):

    probe_data_key = 'dasd'

    def asdict(self, dasd_config):
        if dasd_config.get("type", "ECKD") != "ECKD":
            return None
        dasd_name = os.path.basename(dasd_config['name'])
        device_id = dasd_config['device_id']
        blocksize = dasd_config['blocksize']
        disk_layout = dasd_config['disk_layout']

        return {
            'type': 'dasd',
            'id': f'dasd-{dasd_name}',
            'device_id': device_id,
            'blocksize': blocksize,
            'mode': 'full' if disk_layout == 'not-formatted' else 'quick',
            'disk_layout': disk_layout,
        }

    def parse(self):
        """parse probert 'dasd' data format.

            returns tuple of lists: (configs, errors)
            contain configs of type:dasd and any errors.
        """
        configs = []
        errors = []
        for dasd_name, dasd_config in self.class_data.items():
            if entry := self.asdict(dasd_config):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)


class DmcryptParser(ProbertParser):

    probe_data_key = 'dmcrypt'

    def asdict(self, crypt_config):
        crypt_name = crypt_config['name']
        backing_dev = crypt_config['blkdevs_used']
        if not backing_dev.startswith('/dev/'):
            backing_dev = os.path.join('/dev', backing_dev)

        bdev = self.lookup_devname(backing_dev)
        bdev_data = self.blockdev_data[bdev]
        bdev_id = self.blockdev_to_id(bdev_data) if bdev_data else None
        if not bdev_id:
            raise ValueError(f'Cannot find blockdev id for {bdev}')

        return {
            'type': 'dm_crypt',
            'id': f'dmcrypt-{crypt_name}',
            'volume': bdev_id,
            'key': '',
            'dm_name': crypt_name,
        }

    def parse(self):
        """parse probert 'dmcrypt' data format.

            returns tuple of lists: (configs, errors)
            contain configs of type:dmcrypt and any errors.
        """
        configs = []
        errors = []
        for crypt_name, crypt_config in self.class_data.items():
            if entry := self.asdict(crypt_config):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)


class RaidParser(ProbertParser):

    probe_data_key = 'raid'

    def asdict(self, raid_data):
        devname = raid_data.get('DEVNAME', 'NODEVNAMEKEY')
        # FIXME, need to handle rich md_name values, rather than mdX
        # LP: #1803933
        raidname = os.path.basename(devname)

        action = {
            'type': 'raid',
            'id': self.blockdev_to_id(raid_data),
            'name': raidname,
            'raidlevel': raid_data.get('raidlevel'),
            }

        if 'MD_METADATA' in raid_data:
            action['metadata'] = raid_data["MD_METADATA"]

        if 'container' in raid_data:
            action['container'] = self.blockdev_byid_to_devname(
                raid_data['container'])
        else:
            for k in 'devices', 'spare_devices':
                action[k] = sorted([
                    self.blockdev_byid_to_devname(dev)
                    for dev in raid_data.get(k, [])])

        return action

    def parse(self):
        """parse probert 'raid' data format.

           Collects storage config type: raid for valid
           data and returns tuple of lists, configs, errors.
        """
        configs = []
        errors = []
        for devname, data in self.class_data.items():
            if entry := self.asdict(data):
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)


class MountParser(ProbertParser):

    probe_data_key = 'mount'

    def asdict(self, mdata):
        # the source value may be a devlink alias, look it up
        source = self.lookup_devname(mdata.get('source'))

        # we can filter mounts for block devices only
        # this excludes lots of sys/proc/dev/cgroup
        # mounts that are found but not related to
        # storage config
        # XXX: bind mounts might need some work here
        if not source:
            return {}

        # no floppy, no cdrom
        if self.blockdev_data[source]['MAJOR'] in ["11", "2"]:
            return {}

        source_id = self.blockdev_to_id(self.blockdev_data[source])
        return {
            'type': 'mount',
            'id': f'mount-{source_id}',
            'path': mdata.get('target'),
            'device': f'format-{source_id}',
        }

    def parse(self):
        """parse probert 'mount' data format

           mount : [{.. 'children': [..]}]

           Collects storage config type: mount for valid
           data and returns tuple of lists: (configs, errors)
        """
        def collect_mounts(mdata):
            mounts = [self.asdict(mdata)]
            for child in mdata.get('children', []):
                mounts.extend(collect_mounts(child))
            return [mnt for mnt in mounts if mnt]

        configs = []
        errors = []
        for mdata in self.class_data:
            collected_mounts = collect_mounts(mdata)
            for entry in collected_mounts:
                try:
                    validate_config(entry)
                except ValueError as e:
                    errors.append(e)
                    continue
                configs.append(entry)
        return (configs, errors)


class ZfsParser(ProbertParser):

    probe_data_key = 'zfs'

    def get_local_ds_properties(self, dataset):
        """ extract a dictionary of propertyname: value
            for any property that has a source of 'local'
            which means it's been set by configuration.
        """
        if 'properties' not in dataset:
            return {}

        return {
            prop_name: setting['value']
            for prop_name, setting in dataset['properties'].items()
            if setting['source'] == 'local'
        }

    def zpool_asdict(self, name, zpool_data):
        """ convert zpool data and convert to curtin storage_config dict.
        """
        vdevs = []
        zdb = zpool_data.get('zdb', {})
        for child_name, child_config in zdb.get('vdev_tree', {}).items():
            if not child_name.startswith('children'):
                continue
            path = child_config.get('path')
            if devname := self.blockdev_byid_to_devname(path):
                vdevs.append(devname)

        if not vdevs:
            return None

        id_name = f'zpool-{os.path.basename(vdevs[0])}-{name}'
        return {'type': 'zpool',
                'id': id_name,
                'pool': name,
                'vdevs': sorted(vdevs)}

    def zfs_asdict(self, ds_name, ds_properties, zpool_data):
        # ignore the base pool name (rpool) vs (rpool/ROOT/zfsroot)
        if '/' not in ds_name or not zpool_data:
            return

        id_name = f"zfs-{ds_name.replace('/', '-')}"
        parent_zpool_name = zpool_data.get('pool')
        return {'type': 'zfs',
                'id': id_name,
                'pool': zpool_data.get('id'),
                'volume': ds_name.split(parent_zpool_name)[-1],
                'properties': ds_properties}

    def parse(self):
        """ parse probert 'zfs' data format

            zfs: {
                'zpools': {
                    '<pool1>': {
                        'datasets': {
                            <dataset1>: {
                                "properties": {
                                    "propname": {'source': "default",
                                                 'value': "<value>"},
                                }
                            }
                        }
                        'zdb': {
                            ...
                            vdev_tree: {
                                childrens[N]: {
                                    'path': '/dev/disk/by-id/foo',
                                }
                            }
                            version: 28,
                        }
                    }
                }
            }
        """

        errors = []
        zpool_configs = []
        zfs_configs = []

        for zp_name, zp_data in self.class_data.get('zpools', {}).items():
            zpool_entry = self.zpool_asdict(zp_name, zp_data)
            if zpool_entry:
                try:
                    validate_config(zpool_entry)
                except ValueError as e:
                    errors.append(e)
                    zpool_entry = None

            datasets = zp_data.get('datasets')
            for ds in datasets.keys():
                ds_props = self.get_local_ds_properties(datasets[ds])
                if zfs_entry := self.zfs_asdict(ds, ds_props, zpool_entry):
                    try:
                        validate_config(zfs_entry)
                    except ValueError as e:
                        errors.append(e)
                        continue
                    zfs_configs.append(zfs_entry)

            if zpool_entry:
                zpool_configs.append(zpool_entry)

        return (zpool_configs + zfs_configs, errors)


def ptable_uuid_to_flag_entry(guid):
    name = code = None
    # prefix non-uuid guid values with 0x
    if guid and '-' not in guid and not guid.upper().startswith('0X'):
        guid = f'0x{guid}'
    if guid and guid.upper() in PTABLE_TYPE_MAP:
        name, code = PTABLE_TYPE_MAP[guid.upper()]

    return (name, code)


def extract_storage_config(probe_data, strict=False):
    """ Examine a probert storage dictionary and extract a curtin
        storage configuration that would recreate all of the
        storage devices present in the provided data.

        Returns a storage config dictionary
    """
    convert_map = {
        'bcache': BcacheParser,
        'blockdev': BlockdevParser,
        'dasd': DasdParser,
        'dmcrypt': DmcryptParser,
        'filesystem': FilesystemParser,
        'lvm': LvmParser,
        'raid': RaidParser,
        'mount': MountParser,
        'zfs': ZfsParser,
    }
    configs = []
    errors = []
    LOG.debug('Extracting storage config from probe data')
    for ptype, pname in convert_map.items():
        parser = pname(probe_data)
        found_cfgs, found_errs = parser.parse()
        configs.extend(found_cfgs)
        errors.extend(found_errs)

    LOG.debug('Sorting extracted configurations')
    dasd = [cfg for cfg in configs if cfg.get('type') == 'dasd']
    disk = [cfg for cfg in configs if cfg.get('type') == 'disk']
    part = [cfg for cfg in configs if cfg.get('type') == 'partition']
    format = [cfg for cfg in configs if cfg.get('type') == 'format']
    lvols = [cfg for cfg in configs if cfg.get('type') == 'lvm_volgroup']
    lparts = [cfg for cfg in configs if cfg.get('type') == 'lvm_partition']
    raids = [cfg for cfg in configs if cfg.get('type') == 'raid']
    dmcrypts = [cfg for cfg in configs if cfg.get('type') == 'dm_crypt']
    mounts = [cfg for cfg in configs if cfg.get('type') == 'mount']
    bcache = [cfg for cfg in configs if cfg.get('type') == 'bcache']
    zpool = [cfg for cfg in configs if cfg.get('type') == 'zpool']
    zfs = [cfg for cfg in configs if cfg.get('type') == 'zfs']

    ordered = (dasd + disk + part + format + lvols + lparts + raids +
               dmcrypts + mounts + bcache + zpool + zfs)

    final_config = {'storage': {'version': 2, 'config': ordered}}
    try:
        LOG.info('Validating extracted storage config components')
        validate_config(final_config['storage'])
    except ValueError as e:
        errors.append(e)

    for e in errors:
        LOG.exception('Validation error: %s\n' % e)
    if errors:
        errmsg = "Extract storage config does not validate."
        LOG.warning(errmsg)
        if strict:
            raise RuntimeError(errmsg)

    # build and merge probed data into a valid storage config by
    # generating a config tree for each item in the probed data
    # and then merging the trees, which resolves dependencies
    # and produced a dependency ordered storage config
    LOG.debug("Extracted (unmerged) storage config:\n%s",
              yaml.dump({'storage': ordered},
                        indent=4, default_flow_style=False))

    LOG.debug("Generating storage config dependencies")
    ctrees = []
    for cfg in ordered:
        tree = get_config_tree(cfg.get('id'), final_config)
        ctrees.append(tree)

    LOG.debug("Merging storage config dependencies")
    merged_config = {
        'version': 2,
        'config': merge_config_trees_to_list(ctrees)
    }
    LOG.debug("Merged storage config:\n%s",
              yaml.dump({'storage': merged_config},
                        indent=4, default_flow_style=False))
    return {'storage': merged_config}


# vi: ts=4 expandtab syntax=python
