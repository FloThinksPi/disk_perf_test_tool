import os
import sys
import Queue
import pprint
import logging
import argparse
import threading
import collections

import yaml
from concurrent.futures import ThreadPoolExecutor

import utils
import ssh_utils
import start_vms
import pretty_yaml
from nodes import discover
from nodes.node import Node
from config import cfg_dict, load_config
from sensors.api import start_monitoring
from tests.itest import IOPerfTest, PgBenchTest
from formatters import format_results_for_console


logger = logging.getLogger("io-perf-tool")


def color_me(color):
    RESET_SEQ = "\033[0m"
    COLOR_SEQ = "\033[1;%dm"

    color_seq = COLOR_SEQ % (30 + color)

    def closure(msg):
        return color_seq + msg + RESET_SEQ
    return closure


class ColoredFormatter(logging.Formatter):
    BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, WHITE = range(8)

    colors = {
        'WARNING': color_me(YELLOW),
        'DEBUG': color_me(BLUE),
        'CRITICAL': color_me(YELLOW),
        'ERROR': color_me(RED)
    }

    def __init__(self, msg, use_color=True):
        logging.Formatter.__init__(self, msg)
        self.use_color = use_color

    def format(self, record):
        levelname = record.levelname

        if levelname in self.colors:
            record.levelname = self.colors[levelname](levelname)

        return logging.Formatter.format(self, record)


def setup_logger(logger, level=logging.DEBUG, log_fname=None):
    logger.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setLevel(level)

    log_format = '%(asctime)s - %(levelname)-6s - %(name)s - %(message)s'
    colored_formatter = ColoredFormatter(log_format,
                                         "%H:%M:%S")

    formatter = logging.Formatter(log_format,
                                  "%H:%M:%S")
    sh.setFormatter(colored_formatter)
    logger.addHandler(sh)

    if log_fname is not None:
        fh = logging.FileHandler(log_fname)
        fh.setFormatter(formatter)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)


def format_result(res, formatter):
    data = "\n{0}\n".format("=" * 80)
    data += pprint.pformat(res) + "\n"
    data += "{0}\n".format("=" * 80)
    templ = "{0}\n\n====> {1}\n\n{2}\n\n"
    return templ.format(data, formatter(res), "=" * 80)


class Context(object):
    def __init__(self):
        self.build_meta = {}
        self.nodes = []
        self.clear_calls_stack = []
        self.openstack_nodes_ids = []


def connect_one(node):
    try:
        ssh_pref = "ssh://"
        if node.conn_url.startswith(ssh_pref):
            url = node.conn_url[len(ssh_pref):]
            node.connection = ssh_utils.connect(url)
        else:
            raise ValueError("Unknown url type {0}".format(node.conn_url))
    except Exception:
        logger.exception("During connect to {0}".format(node))
        raise


def connect_all(nodes):
    logger.info("Connecting to nodes")
    with ThreadPoolExecutor(32) as pool:
        list(pool.map(connect_one, nodes))
    logger.info("All nodes connected successfully")


def save_sensors_data(q):
    logger.info("Start receiving sensors data")
    sensor_data = []
    while True:
        val = q.get()
        if val is None:
            q.put(sensor_data)
            break
        sensor_data.append(val)
    logger.info("Sensors thread exits")


def test_thread(test, node, barrier, res_q):
    try:
        logger.debug("Run preparation for {0}".format(node.conn_url))
        test.pre_run(node.connection)
        logger.debug("Run test for {0}".format(node.conn_url))
        test.run(node.connection, barrier)
    except Exception as exc:
        logger.exception("In test {0} for node {1}".format(test, node))
        res_q.put(exc)


def run_tests(config, nodes):
    tool_type_mapper = {
        "io": IOPerfTest,
        "pgbench": PgBenchTest,
    }

    test_nodes = [node for node in nodes
                  if 'testnode' in node.roles]

    res_q = Queue.Queue()

    for test_block in config:
        for name, params in test_block.items():
            logger.info("Starting {0} tests".format(name))

            threads = []
            barrier = utils.Barrier(len(test_nodes))
            for node in test_nodes:
                msg = "Starting {0} test on {1} node"
                logger.debug(msg.format(name, node.conn_url))
                test = tool_type_mapper[name](params, res_q.put)
                th = threading.Thread(None, test_thread, None,
                                      (test, node, barrier, res_q))
                threads.append(th)
                th.daemon = True
                th.start()

            def gather_results(res_q, results):
                while not res_q.empty():
                    val = res_q.get()

                    if isinstance(val, Exception):
                        msg = "Exception during test execution: {0}"
                        raise ValueError(msg.format(val.message))

                    results.append(val)

            results = []

            while True:
                for th in threads:
                    th.join(1)
                    gather_results(res_q, results)

                if all(not th.is_alive() for th in threads):
                    break

            gather_results(res_q, results)
            yield name, test.merge_results(results)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run disk io performance test")

    parser.add_argument("-l", dest='extra_logs',
                        action='store_true', default=False,
                        help="print some extra log info")

    parser.add_argument("-b", '--build_description',
                        type=str, default="Build info")
    parser.add_argument("-i", '--build_id', type=str, default="id")
    parser.add_argument("-t", '--build_type', type=str, default="GA")
    parser.add_argument("-u", '--username', type=str, default="admin")
    parser.add_argument("-p", '--post-process-only', default=None)
    parser.add_argument("-o", '--output-dest', nargs="*")
    parser.add_argument("config_file", nargs="?", default="config.yaml")

    return parser.parse_args(argv[1:])


def log_nodes_statistic(_, ctx):
    nodes = ctx.nodes
    logger.info("Found {0} nodes total".format(len(nodes)))
    per_role = collections.defaultdict(lambda: 0)
    for node in nodes:
        for role in node.roles:
            per_role[role] += 1

    for role, count in sorted(per_role.items()):
        logger.debug("Found {0} nodes with role {1}".format(count, role))


def log_sensors_config(cfg):
    pass


def connect_stage(cfg, ctx):
    ctx.clear_calls_stack.append(disconnect_stage)
    connect_all(ctx.nodes)


def discover_stage(cfg, ctx):
    if cfg.get('discover') is not None:
        discover_objs = [i.strip() for i in cfg['discover'].strip().split(",")]
        ctx.nodes.extend(discover.discover(discover_objs, cfg['clouds']))

    for url, roles in cfg.get('explicit_nodes', {}).items():
        ctx.nodes.append(Node(url, roles.split(",")))


def deploy_sensors_stage(cfg_dict, ctx):
    if 'sensors' not in cfg_dict:
        return

    ctx.clear_calls_stack.append(remove_sensors_stage)
    cfg = cfg_dict.get('sensors')
    sens_cfg = []

    for role, sensors_str in cfg["roles_mapping"].items():
        sensors = [sens.strip() for sens in sensors_str.split(",")]

        collect_cfg = dict((sensor, {}) for sensor in sensors)

        for node in ctx.nodes:
            if role in node.roles:
                sens_cfg.append((node.connection, collect_cfg))

    log_sensors_config(sens_cfg)

    ctx.sensor_cm = start_monitoring(cfg["receiver_uri"], None,
                                     connected_config=sens_cfg)

    ctx.sensors_control_queue = ctx.sensor_cm.__enter__()

    th = threading.Thread(None, save_sensors_data, None,
                          (ctx.sensors_control_queue,))
    th.daemon = True
    th.start()
    ctx.sensor_listen_thread = th


def remove_sensors_stage(cfg, ctx):
    ctx.sensors_control_queue.put(None)
    ctx.sensor_listen_thread.join()
    ctx.sensor_data = ctx.sensors_control_queue.get()


def run_all_test(cfg, ctx):
    ctx.results = []

    if 'start_test_nodes' in cfg['tests']:
        params = cfg['tests']['start_test_nodes']['openstack']
        os_nodes_ids = []

        os_creds = params['creds']

        if os_creds == 'fuel':
            raise NotImplementedError()

        elif os_creds == 'clouds':
            os_cfg = cfg['clouds']['openstack']
            tenant = os_cfg['tenant_name'].strip()
            user = os_cfg['username'].strip()
            passwd = os_cfg['password'].strip()
            auth_url = os_cfg['os_auth_url'].strip()

        elif os_creds == 'ENV':
            tenant = None
            user = None
            passwd = None
            auth_url = None

        else:
            raise ValueError("Only 'ENV' creds are supported")

        start_vms.nova_connect(user, passwd, tenant, auth_url)

        logger.info("Preparing openstack")
        start_vms.prepare_os(user, passwd, tenant, auth_url)

        new_nodes = []
        for new_node, node_id in start_vms.launch_vms(params):
            new_node.roles.append('testnode')
            ctx.nodes.append(new_node)
            os_nodes_ids.append(node_id)
            new_nodes.append(new_node)

        store_nodes_in_log(cfg, os_nodes_ids)
        ctx.openstack_nodes_ids = os_nodes_ids

        connect_all(new_nodes)

    if 'tests' in cfg:
        ctx.results.extend(run_tests(cfg['tests'], ctx.nodes))


def shut_down_vms_stage(cfg, ctx):
    vm_ids_fname = cfg_dict['vm_ids_fname']
    if ctx.openstack_nodes_ids is None:
        nodes_ids = open(vm_ids_fname).read().split()
    else:
        nodes_ids = ctx.openstack_nodes_ids

    if len(nodes_ids) != 0:
        logger.info("Removing nodes")
        start_vms.clear_nodes(nodes_ids)
        logger.info("Nodes has been removed")

    if os.path.exists(vm_ids_fname):
        os.remove(vm_ids_fname)


def store_nodes_in_log(cfg, nodes_ids):
    with open(cfg['vm_ids_fname'], 'w') as fd:
        fd.write("\n".join(nodes_ids))


def clear_enviroment(cfg, ctx):
    if os.path.exists(cfg_dict['vm_ids_fname']):
        shut_down_vms_stage(cfg, ctx)


def run_tests_stage(cfg, ctx):
    # clear nodes that possible were created on previous test running
    # clear_enviroment(cfg, ctx) << fix OS connection
    ctx.clear_calls_stack.append(shut_down_vms_stage)
    run_all_test(cfg, ctx)


def disconnect_stage(cfg, ctx):
    ssh_utils.close_all_sessions()

    for node in ctx.nodes:
        if node.connection is not None:
            node.connection.close()


def yamable(data):
    if isinstance(data, (tuple, list)):
        return map(yamable, data)

    if isinstance(data, unicode):
        return str(data)

    if isinstance(data, dict):
        res = {}
        for k, v in data.items():
            res[yamable(k)] = yamable(v)
        return res

    return data


def store_raw_results_stage(cfg, ctx):

    raw_results = os.path.join(cfg_dict['var_dir'], 'raw_results.yaml')

    if os.path.exists(raw_results):
        cont = yaml.load(open(raw_results).read())
    else:
        cont = []

    cont.extend(yamable(ctx.results))
    raw_data = pretty_yaml.dumps(cont)

    with open(raw_results, "w") as fd:
        fd.write(raw_data)


def console_report_stage(cfg, ctx):
    for tp, data in ctx.results:
        if 'io' == tp:
            print format_results_for_console(data)


def report_stage(cfg, ctx):
    # html_report = report.make_io_report(ctx.results)
    # html_rep_fname = cfg['html_report_file']

    # with open(html_rep_fname, "w") as fd:
    #     fd.write(html_report)

    # logger.info("Html report were stored in " + html_rep_fname)

    text_rep_fname = cfg_dict['text_report_file']
    with open(text_rep_fname, "w") as fd:
        for tp, data in ctx.results:
            if 'io' == tp:
                fd.write(format_results_for_console(data))
                fd.write("\n")
                fd.flush()

    logger.info("Text report were stored in " + text_rep_fname)


def complete_log_nodes_statistic(cfg, ctx):
    nodes = ctx.nodes
    for node in nodes:
        logger.debug(str(node))


def load_data_from(var_dir):
    def load_data_from_file(cfg, ctx):
        raw_results = os.path.join(var_dir, 'raw_results.yaml')
        print "load data from", raw_results
        ctx.results = yaml.load(open(raw_results).read())
    return load_data_from_file


def main(argv):
    opts = parse_args(argv)

    if opts.post_process_only is not None:
        stages = [
            load_data_from(opts.post_process_only),
            console_report_stage,
            report_stage
        ]
    else:
        stages = [
            discover_stage,
            log_nodes_statistic,
            connect_stage,
            deploy_sensors_stage,
            run_tests_stage,
            store_raw_results_stage,
            console_report_stage,
            report_stage
        ]

    load_config(opts.config_file)

    level = logging.DEBUG if opts.extra_logs else logging.WARNING
    setup_logger(logger, level, cfg_dict['log_file'])

    logger.info("All info would be stored into {0}".format(
        cfg_dict['var_dir']))

    ctx = Context()
    ctx.build_meta['build_id'] = opts.build_id
    ctx.build_meta['build_descrption'] = opts.build_description
    ctx.build_meta['build_type'] = opts.build_type
    ctx.build_meta['username'] = opts.username

    try:
        for stage in stages:
            logger.info("Start {0.__name__} stage".format(stage))
            stage(cfg_dict, ctx)
    finally:
        exc, cls, tb = sys.exc_info()
        for stage in ctx.clear_calls_stack[::-1]:
            try:
                logger.info("Start {0.__name__} stage".format(stage))
                stage(cfg_dict, ctx)
            except Exception as exc:
                logger.exception("During {0.__name__} stage".format(stage))

        if exc is not None:
            raise exc, cls, tb

    logger.info("All info stotored into {0}".format(cfg_dict['var_dir']))
    return 0


if __name__ == '__main__':
    exit(main(sys.argv))
