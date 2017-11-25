"""Implement configuration file parsing."""

# Copyright (C) 2015 Brad Cowie, Christopher Lorier and Joe Stringer.
# Copyright (C) 2015 Research and Education Advanced Network New Zealand Ltd.
# Copyright (C) 2015--2017 The Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import re

from faucet import config_parser_util
from faucet.acl import ACL
from faucet.dp import DP
from faucet.meter import Meter
from faucet.port import Port
from faucet.router import Router
from faucet.valve_of import MIN_VID, MAX_VID
from faucet.vlan import VLAN
from faucet.watcher_conf import WatcherConf


V2_TOP_CONFS = (
    'acls',
    'dps',
    'meters',
    'routers',
    'vlans')


class InvalidConfigError(Exception):
    """This error is thrown when the config file is not valid"""
    pass


def dp_parser(config_file, logname):
    logger = config_parser_util.get_logger(logname)
    conf = config_parser_util.read_config(config_file, logname)
    config_hashes = None
    dps = None

    try:
        assert conf is not None, 'Config file is empty'
        assert type(conf) is dict, 'Config file does not have valid syntax'
        version = conf.pop('version', 2)
        assert version == 2, 'Only config version 2 is supported'
        config_hashes, dps = _config_parser_v2(config_file, logname)
        assert dps is not None, 'dps are not defined'
    
    except AssertionError as err:
        raise InvalidConfigError(err)

    return config_hashes, dps

def _dp_parser_v2(logger, acls_conf, dps_conf, meters_conf,
                  routers_conf, vlans_conf):
    dps = []
    vid_dp = collections.defaultdict(set)

    def _get_vlan_by_identifier(dp_id, vlan_ident, vlans):
        if vlan_ident in vlans:
            return vlans[vlan_ident]
        for vlan in list(vlans.values()):
            if vlan_ident == str(vlan.vid):
                return vlan
        try:
            vid = int(str(vlan_ident), 0)
        except ValueError:
            assert False, 'VLAN VID value (%s) is invalid' % vlan_ident

        assert vid >= MIN_VID and vid <= MAX_VID, 'VLAN %s VID value %d is not in valid range' % (
            vlan_ident, vid)

        return vlans.setdefault(vlan_ident, VLAN(vid, dp_id))

    def _dp_add_vlan(dp, vlan):
        if vlan not in dp.vlans:
            dp.add_vlan(vlan)
            vid_dp[vlan.vid].add(dp.name)

            if len(vid_dp[vlan.vid]) > 1:
                assert not vlan.bgp_routerid, (
                    'DPs %s sharing a BGP speaker VLAN is unsupported' % (
                        str.join(', ', vid_dp[vlan.vid])))

    def _dp_parse_port(dp_id, p_identifier, port_conf, vlans):
        port = Port(p_identifier, port_conf)

        if port.native_vlan is not None:
            v_identifier = port.native_vlan
            vlan = _get_vlan_by_identifier(dp_id, v_identifier, vlans)
            port.native_vlan = vlan
            vlan.add_untagged(port)
        port_tagged_vlans = [
            _get_vlan_by_identifier(dp_id, v_identifier, vlans) for v_identifier in port.tagged_vlans]
        port.tagged_vlans = port_tagged_vlans
        for vlan in port.tagged_vlans:
            vlan.add_tagged(port)
        assert port.vlans() or port.stack, '%s must have a VLAN or be a stack port' % port
        return port

    def _dp_add_ports(dp, dp_conf, dp_id, vlans):
        ports_conf = dp_conf.pop('interfaces', {})
        port_ranges_conf = dp_conf.pop('interface-ranges', {})
        # as users can config port vlan by using vlan name, we store vid in
        # Port instance instead of vlan name for data consistency
        port_num_to_port_conf = {}
        for port_ident, port_conf in list(ports_conf.items()):
            if 'number' in port_conf:
                port_num = port_conf['number']
            else:
                port_num = port_ident
            port_num_to_port_conf[port_num] = (port_ident, port_conf)
        for port_range, port_conf in list(port_ranges_conf.items()):
            # port range format: 1-6 OR 1-6,8-9 OR 1-3,5,7-9
            port_nums = set()
            if 'number' in port_conf:
                del port_conf['number']
            for range_ in re.findall(r'(\d+-\d+)', port_range):
                start_num = end_num = 0
                try:
                    start_num, end_num = [int(num) for num in range_.split('-')]
                except:
                    assert False, "port range must be integer"
                assert start_num < end_num, (
                    'Incorrect port range (%d - %d)' % (start_num, end_num))
                port_nums.update(list(range(start_num, end_num + 1)))
                port_range = re.sub(range_, '', port_range)
            other_nums = [int(p) for p in re.findall(r'\d+', port_range)]
            port_nums.update(other_nums)
            for port_num in port_nums:
                if port_num in port_num_to_port_conf:
                    # port range config has lower priority than individual port config
                    for attr, value in list(port_conf.items()):
                        port_num_to_port_conf[port_num][1].setdefault(attr, value)
                else:
                    port_num_to_port_conf[port_num] = (port_num, port_conf)
        for port_num, port_conf in list(port_num_to_port_conf.values()):
            port = _dp_parse_port(dp_id, port_num, port_conf, vlans)
            dp.add_port(port)
        for vlan in list(vlans.values()):
            if vlan.get_ports():
                _dp_add_vlan(dp, vlan)

    for identifier, dp_conf in list(dps_conf.items()):
        dp = DP(identifier, dp_conf)
        dp.sanity_check()
        dp_id = dp.dp_id

        vlans = {}
        for vlan_ident, vlan_conf in list(vlans_conf.items()):
            vlans[vlan_ident] = VLAN(vlan_ident, dp_id, vlan_conf)
        acls = []
        for acl_ident, acl_conf in list(acls_conf.items()):
            acls.append((acl_ident, ACL(acl_ident, acl_conf)))
        for router_ident, router_conf in list(routers_conf.items()):
            router = Router(router_ident, router_conf)
            dp.add_router(router_ident, router)
        for meter_ident, meter_conf in list(meters_conf.items()):
            dp.meters[meter_ident] = Meter(meter_ident, meter_conf)
        _dp_add_ports(dp, dp_conf, dp_id, vlans)
        for acl_ident, acl in acls:
            dp.add_acl(acl_ident, acl)
        dps.append(dp)

    for dp in dps:
        dp.finalize_config(dps)
    for dp in dps:
        dp.resolve_stack_topology(dps)

    return dps


def _config_parser_v2(config_file, logname):
    logger = config_parser_util.get_logger(logname)
    config_path = config_parser_util.dp_config_path(config_file)
    top_confs = {}
    config_hashes = {}
    dps = None
    for top_conf in V2_TOP_CONFS:
        top_confs[top_conf] = {}

    if not config_parser_util.dp_include(
            config_hashes, config_path, logname, top_confs):
        assert False, 'Error found while loading config file: %s' % config_path 
    elif not top_confs['dps']:
        assert False, 'DPs not configured in file: %s' % config_path
    else:
        dps = _dp_parser_v2(
            logger,
            top_confs['acls'],
            top_confs['dps'],
            top_confs['meters'],
            top_confs['routers'],
            top_confs['vlans'])
    return (config_hashes, dps)


def get_config_for_api(valves):
    config = {}
    for i in V2_TOP_CONFS:
        config[i] = {}
    for valve in list(valves.values()):
        valve_conf = valve.get_config_dict()
        for i in V2_TOP_CONFS:
            if i in valve_conf:
                config[i].update(valve_conf[i])
    return config


def watcher_parser(config_file, logname, prom_client):
    conf = config_parser_util.read_config(config_file, logname)
    return _watcher_parser_v2(conf, logname, prom_client)


def _watcher_parser_v2(conf, logname, prom_client):
    logger = config_parser_util.get_logger(logname)
    result = []

    dps = {}
    for faucet_file in conf['faucet_configs']:
        _, dp_list = dp_parser(faucet_file, logname)
        if dp_list:
            for dp in dp_list:
                dps[dp.name] = dp

    dbs = conf.pop('dbs')

    for name, dictionary in list(conf['watchers'].items()):
        for dp_name in dictionary['dps']:
            if dp_name not in dps:
                logger.error('dp %s metered but not configured', dp_name)
                continue
            dp = dps[dp_name]
            watcher = WatcherConf(name, dictionary, prom_client)
            watcher.add_db(dbs[watcher.db])
            watcher.add_dp(dp)
            result.append(watcher)

    return result
