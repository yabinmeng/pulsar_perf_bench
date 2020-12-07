import os
import sys
import subprocess
import argparse
import yaml
import re
import socket
import logging
import shutil
import glob

from os import path
from datetime import datetime, timezone

_CONSUMER_THRUPT_METRICS_NAMES = ['thrupt_msg/s', 'thrupt_Mbit/s']
_PRODUCER_THRUPT_METRICS_NAMES = ['thrupt_msg/s', 'thrupt_Mbit/s', 'thrupt_failure_msg/s']
_LATENCY_METRICS_NAMES = ['latency_mean', 'latency_med', 'latency_95pct', 'latency_99pct',
                          'latency_99.9pct', 'latency_99.99pct', 'latency_Max']

_DT_FMT = "%Y-%m-%d"
_TM_FMT = "%H:%M:%S"
_TM_FMT_MILLI = "%H:%M:%S.%f"
_DTTM_FMT = _DT_FMT + " " + _TM_FMT
_DTTM_FMT2 = _DT_FMT + "_" + _TM_FMT
_DTTM_FMT_MILLI = _DT_FMT + " " + _TM_FMT_MILLI

_INVALID_GRAPHITE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

_PULSAR_CMD_OUTPUT_SEPERATOR = "-------------------------------"


##
# Error exit helper function
##
def _error_exit(err_cd, err_msg, print_help):
    print(">> {}".format(err_msg))
    if print_help:
        print("------------------------------")
        parser.print_help()
    sys.exit(err_cd)


##
# Combine multiple list into one
##
def _combine_list(*lists):
    combined_list = []
    for item in lists:
        combined_list = combined_list + item
    return combined_list


##
# Check whether Prometheus Graphite exporter is valid
##
def _chk_graphite_port(portstr):
    valid_port = True
    error_msg = ""

    # graphite port must be in format <host_ip_or_name>:<port_number>
    if not re.match('[\\w.]+:[0-9]+', portstr):
        valid_port = False
        error_msg = "Invalid prometheus graphite exporter (-g/--prom_graphite) format. Valid format: \"<host>:<port>\""
    else:
        host, port = portstr.split(':')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((host, int(port)))
        except Exception as ex:
            valid_port = False
            error_msg = "Specified host and port can't be reached ({}).".format(repr(ex))
        finally:
            s.close()

    return valid_port, error_msg


##
# Generate "pulsar-perf" command option string from configuration setting dictionary
##
def _gen_pulsar_perf_cmdopt_str(settings):
    cmd_optstr = ""
    for key, value in settings.items():
        if value is not None and value != "":
            cmd_optstr = cmd_optstr + "--" + key + " " + str(value) + " "
    return cmd_optstr.rstrip()


##
# Get today's date and/or time string in default timezone
##
def _get_dttm_str(fmt):
    dttm_str = datetime.now().strftime(fmt)
    return dttm_str


##
# Get today's date and/or time string in UTC timezone
##
def _get_dttm_str_utc(fmt):
    dttm_str = datetime.now(timezone.utc).strftime(fmt)
    return dttm_str


##
# Parse out throughput/latency related metrics from the output line
##
def _get_metrics_ts(time_str):
    dttm_str = _get_dttm_str_utc(_DT_FMT) + " " + time_str
    dttm = datetime.strptime(dttm_str, _DTTM_FMT_MILLI)
    ts = int(datetime.timestamp(dttm))
    return ts


##
# Parse out throughput/latency related metrics from the output line
##
def _parse_metrics_line(metrics_line):
    metrics_items = re.findall('\\d+.\\d+\\b', metrics_line)
    return metrics_items


##
# Sanitize characters that are not recognized by Graphite
##
def _sanitize(s):
    return _INVALID_GRAPHITE_CHARS.sub('_', s)


##
# pulsar-perf produce metrics line handler
##
class MetricsLineHandler:
    def __init__(self, sokt, rm_file, gm_file, prefix, clnt_type, m_names, line, mline_tag):
        self.sokt = sokt
        self.rm_file = rm_file
        self.gm_file = gm_file
        self.prefix = prefix
        self.client_type = clnt_type
        self.m_names = m_names
        self.line = line
        self.mline_tag = mline_tag

    def process(self):
        has_metrics = False

        if self.line.find("{}".format(self.mline_tag)) != -1:
            has_metrics = True

            metrics_time_str = self.line.split(' ', 1)[0]
            metrics_ts = _get_metrics_ts(metrics_time_str)

            thrupt_pos = self.line.find(self.mline_tag)
            latency_pos = self.line.find("Latency:")

            thrupt_str = self.line[thrupt_pos:latency_pos]
            latency_str = self.line[latency_pos:len(self.line)]

            thrupt_metrics_list = _parse_metrics_line(thrupt_str)
            latency_metrics_list = _parse_metrics_line(latency_str)

            ##
            # Write metrics to a CSV file
            # - metrics value line
            self.rm_file.write("{},{},{}\n".format(
                str(metrics_ts),
                ",".join(map(str, thrupt_metrics_list)),
                ",".join(map(str, latency_metrics_list))))

            ##
            # Write metrics to a Graphite exporter
            if self.sokt is not None:
                metrics_value_list = _combine_list(thrupt_metrics_list, latency_metrics_list)
                gmetrics_cnt_per_line = len(metrics_value_list)

                i = 0
                while i < gmetrics_cnt_per_line:
                    metrics_name = self.m_names[i]
                    metrics_value = metrics_value_list[i]
                    graphite_metrics_str = "{}_{};clnt_type={} {} {}".format(
                        self.prefix,
                        _sanitize(metrics_name),
                        self.client_type,
                        metrics_value,
                        metrics_ts
                    )

                    self.gm_file.write(graphite_metrics_str + "\n")
                    self.sokt.send("{}\r\n".format(graphite_metrics_str).encode('ascii'))

                    i = i + 1

        return has_metrics


##
# Execute "pulsar-perf produce command
##
def _exec_pulsar_perf_cmd(pperf_cmd_timeout, cmdstr, subcmd, rm_file, gm_file, graphite_port, gmetrics_prefix):
    cmd_start_time = datetime.now()

    p = subprocess.Popen(
        [cmdstr],
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    metrics_names = []
    if subcmd == "produce":
        metrics_names = _combine_list(_PRODUCER_THRUPT_METRICS_NAMES, _LATENCY_METRICS_NAMES)
    elif subcmd == "consume":
        metrics_names = _combine_list(_CONSUMER_THRUPT_METRICS_NAMES, _LATENCY_METRICS_NAMES)

    assert (len(metrics_names) > 0)

    ##
    # Open a socket to remote graphite reporter
    sokt = None
    if graphite_port != "":
        sokt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        host, port_str = graphite_port.split(':')
        port = int(port_str)
        sokt.connect((host, port))

    print_title_line = True
    while True:
        line = p.stdout.readline()

        # This only works for producer and doesn't work for consumer
        #   because consumer doesn't honor "--test-duration" parameter
        if not line:
            break

        cur_time = datetime.now()
        cmd_exec_time = cur_time - cmd_start_time
        if cmd_exec_time.total_seconds() > (pperf_cmd_timeout + 10):
            break

        if not line.strip() == "":
            logger_pulsar_perf.debug(line.strip())

        if subcmd == "produce":
            title_line = "time,{},{}\n".format(
                ",".join(_PRODUCER_THRUPT_METRICS_NAMES),
                ",".join(_LATENCY_METRICS_NAMES))
            metrics_line_identifier = "Throughput produced:"
        else:
            title_line = "time,{},{}\n".format(
                ",".join(_PRODUCER_THRUPT_METRICS_NAMES),
                ",".join(_LATENCY_METRICS_NAMES))
            metrics_line_identifier = "Throughput received:"

        metrics_line_handler = MetricsLineHandler(sokt,
                                                  rm_file,
                                                  gm_file,
                                                  gmetrics_prefix,
                                                  subcmd,
                                                  metrics_names,
                                                  line,
                                                  metrics_line_identifier)

        # - header line
        if print_title_line:
            rm_file.write(title_line)
            print_title_line = False

        line_processed = metrics_line_handler.process()

    p.terminate()

    if sokt is not None:
        sokt.close()


##
# Execute "pulsar-admin" command
##
def _exec_pulsar_adm_cmd(cmdstr, show_cmd_output=True, keyword_to_chk=None):
    logger_pulsar_admin.debug("      ({})".format(cmdstr))

    p = subprocess.Popen(
        [cmdstr],
        shell=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    # success HTTP code
    http_cd = 200
    http_rsnstr = ""

    warn_msg_to_skip = "Warning: Nashorn engine"

    have_output = False
    keyword_exists = False
    while True:
        line = p.stdout.readline()
        line = line.strip()

        if not line:
            break
        elif warn_msg_to_skip not in line:
            have_output = True

            if show_cmd_output:
                logger_pulsar_admin.debug(line.strip())

            # check if keyword exists
            if not keyword_exists and keyword_to_chk is not None and keyword_to_chk in line:
                keyword_exists = True

            # check for HTTP code
            regfindall = re.findall('HTTP [0-9]+ [a-zA-Z]+', line.strip())
            if regfindall:
                http_cdstr = regfindall[0]
                http_cd = int(http_cdstr.split()[1])

            # check for reason string
            if re.match('^Reason: \\w+', line):
                http_rsnstr = line

    return http_cd, http_rsnstr, keyword_exists


##
# Process the generated hgrm file from pulsar-perf
##
def _process_hgrm_result_file(pbin_homedir, pperf_exec_nm):
    for file in glob.glob("{}/*.hgrm".format(pbin_homedir)):
        shutil.move(file, "metrics/{}.hgrm".format(pperf_exec_nm))


##
# Main program logic
##
if __name__ == '__main__':
    print()

    if not os.path.exists("logs"):
        os.mkdir("logs")
    if not os.path.exists("metrics"):
        os.mkdir("metrics")

    ##
    # Set up a logger with 2 handlers: one log file and one on screen
    ##
    log_file_name = "logs/pperf_bench_" + _get_dttm_str_utc(_DTTM_FMT2) + ".log"
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt=_DTTM_FMT,
                        filename=log_file_name,
                        filemode='w')
    logger = logging.getLogger('Main')

    cnsl_handler = logging.StreamHandler()
    cnsl_handler.setLevel(logging.INFO)
    cnsl_formatter = logging.Formatter('%(message)s')
    cnsl_handler.setFormatter(cnsl_formatter)
    logger.addHandler(cnsl_handler)

    logger_pulsar_admin = logging.getLogger('pulsar-admin')
    logger_pulsar_perf = logging.getLogger('pular-perf')

    ##
    # Process input parameters
    ##
    parser = argparse.ArgumentParser(prog='pperf_bench.py')
    parser.add_argument(
        '-f', '--config', nargs='?', default='./ppfb.yaml', action='append',
        help='benchmark configuration file (default: \"ppfb.yaml\" file under the same directory).')
    parser.add_argument(
        '-d', '--duration', nargs='?', default='10m',
        help="benchmark execution duration (format: <integer_value>[h|m|s], default: 10m).")
    parser.add_argument(
        '-t', '--topic',
        help="pulsar topic name (format: \"<tenant>/<namespace>/<topic>\").")
    parser.add_argument(
        '-g', '--prom_graphite', nargs='?',
        help="Prometheus graphite exporter host and port (format: <host_ip>:9109")
    arg_ns, unknown = parser.parse_known_args()

    # parameter "-f/--config"
    config_yaml_file = ""
    if not path.exists(arg_ns.config):
        _error_exit(10, "Can't find specified configuration file: \"{}\".".format(arg_ns.config), True)
    else:
        config_yaml_file = arg_ns.config

    # parameter "-d/--duration"
    duration_in_sec = 600
    if arg_ns.duration is not None:
        arg_duration_str = arg_ns.duration
        time_unit = arg_duration_str[-1]

        try:
            arg_duration_intval = int(arg_duration_str[0:len(arg_duration_str) - 1])
            if time_unit == 's':
                duration_in_sec = arg_duration_intval
            elif time_unit == 'm':
                duration_in_sec = arg_duration_intval * 60
            elif time_unit == 'h':
                duration_in_sec = arg_duration_intval * 3600
            else:
                duration_in_sec = int("wrong_format")
        except ValueError as verr:
            _error_exit(
                20, "Invalid duration (\"-d/--duration\") value format. Valid format: \"<integer_value>[h|m|s]\"", True)

    # parameter "-t/--topic"
    full_topic_name = ""
    tenant_name = ""
    namespace_name = ""
    if arg_ns.topic is not None:
        # topic must be in "<tenant_name>/<namespace_name>/<topic_name>"
        if not re.match('[0-9a-zA-Z]+(/[0-9a-zA-Z]+){2}', arg_ns.topic):
            _error_exit(30, "Invalid topic (-t/--topic) name format. Valid format: \"<tenant>/<namespace>/<topic>\"",
                        True)

        full_topic_name = arg_ns.topic
        topic_parts = full_topic_name.split('/')
        tenant_name = topic_parts[0]
        namespace_name = topic_parts[1]
    else:
        _error_exit(40, "Topic (-t/--topic) name is mandatory.", True)

    # parameter "-g/--prom_graphite"
    prom_graphite_port = ""
    if arg_ns.prom_graphite is not None:
        valid, errmsg = _chk_graphite_port(arg_ns.prom_graphite)

        if valid:
            prom_graphite_port = arg_ns.prom_graphite
        else:
            _error_exit(50, errmsg, True)

    ##
    # Parse the specified config file (YAML format)
    ##
    config_category = ['pfb-connection',
                       'pfb-general',
                       'pfb-persistence',
                       'pulsar-perf-common',
                       'pulsar-perf-producer',
                       'pulsar-perf-consumer']

    ###
    # Load config YAML file
    with open(config_yaml_file) as f:
        config_data = yaml.load(f, Loader=yaml.FullLoader)

    # General settings for pulsar perf benchmark testing
    pfb_general_settings = config_data['pfb-general']
    # print(pfb_general_settings)

    ###
    # Check settings under "pfb-general" section
    pulsar_bin_homedir = pfb_general_settings['pulsar_bin_homedir']
    pulsar_admin_bin = pulsar_bin_homedir + "/bin/pulsar-admin"
    pulsar_perf_bin = pulsar_bin_homedir + "/bin/pulsar-perf"

    if not (path.exists(pulsar_admin_bin) and path.exists(pulsar_perf_bin)):
        _error_exit(60, "Can't find \"pulsar-admin\" or \"pulsar-perf\" commands.", False)

    # Cluster name can't be empty
    cluster_name = pfb_general_settings['cluster_name']
    if cluster_name == "":
        _error_exit(70, "\"cluster_name\" can't be empty", False)

    # topic characteristics: persistent/non-persistent, regular/partitioned
    topic_pers_str = pfb_general_settings['topic_type'].lower()
    valid_topic_pers_strings = ['persistent', 'non-persistent']
    if topic_pers_str not in valid_topic_pers_strings:
        _error_exit(80, "Incorrect setting of \"topic_type\". Valid values: {}".format(valid_topic_pers_strings), False)

    partitioned = pfb_general_settings['partitioned_topic']
    _num_partitions = 0
    try:
        _num_partitions = int(pfb_general_settings['num_partitions'])
    except ValueError as verr:
        _error_exit(90, "Incorrect setting of \"num_partitions\". Must be an integer.", False)

    client_type = pfb_general_settings['client_type'].lower()
    valid_client_types = ['producer', 'consumer']
    if client_type not in valid_client_types:
        _error_exit(100, "Incorrect setting of \"client_type\". Valid values: {}".format(valid_client_types), False)

    real_topic_name = topic_pers_str + "://" + full_topic_name

    ###
    # Check Pulsar persistence settings if needed.
    pfb_persistence_settings = config_data['pfb-persistence']
    persistence_enabled = pfb_persistence_settings['enabled']
    ensemble_size = 0
    write_quorum = 0
    ack_quorum = 0
    dedup_enabled = False
    if persistence_enabled:
        ensemble_size = int(pfb_persistence_settings['ensembleSize'])
        write_quorum = int(pfb_persistence_settings['writeQuorum'])
        ack_quorum = int(pfb_persistence_settings['ackQuorum'])
        dedup_enabled = pfb_persistence_settings['deduplicationEnabled']

        # Rules:
        #  - write_quorum and ack_quorum MUST be equal to or less than ensemble_size
        #  - ack_quorum should NOT be set as bigger than write_quorum.
        if write_quorum > ensemble_size or ack_quorum > ensemble_size or ack_quorum > write_quorum:
            _error_exit(110,
                        "Incorrect \"ensembleSize,writeQuorum,ackQuorum\" settings ({},{},{}).\n"
                        "   - ensembleSize must be no less than writeQuorum or ackQuorum.\n"
                        "   - ackQuorum shouldn't be larger than writeQuorum".format(
                            ensemble_size, write_quorum, ack_quorum),
                        False)

    ###
    # Start submitting the workload to the Pulsar instance
    #
    cmd_output_cnt = 1

    ###
    # Check if the Pulsar tenant exists; if not, create it
    #
    logger.info("{}. Check if Pulsar tenant \"{}\" exists".format(
        cmd_output_cnt, tenant_name))

    pulsar_admin_cmd_str = "{} tenants list".format(pulsar_admin_bin)
    http_code, reason_str, tenant_exists = _exec_pulsar_adm_cmd(pulsar_admin_cmd_str, False, tenant_name)

    if not tenant_exists:
        logger.info("   >> Pulsar Tenant \"{}\" doesn't exist; create it!".format(tenant_name))
        pulsar_admin_cmd_str = "{} tenants create {}".format(pulsar_admin_bin, tenant_name)
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str, False)
    else:
        logger.info("   >> Pulsar Tenant \"{}\" already exists".format(tenant_name))

    logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR + "\n")
    cmd_output_cnt = cmd_output_cnt + 1

    ###
    # Check if the Pulsar namespace exists under the tenant; if not, create it
    #
    logger.info("{}. Check if Pulsar namespace \"{}\" exists under tenant \"{}\"".format(
        cmd_output_cnt, namespace_name, tenant_name))

    pulsar_admin_cmd_str = "{} namespaces list {}".format(pulsar_admin_bin, tenant_name)
    http_code, reason_str, namespace_exists = \
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str, False, "{}/{}".format(tenant_name, namespace_name))
    if not namespace_exists:
        logger.info("   >> Pulsar namespace \"{}\" under tenant \"{}\" doesn't exist; create it!".format(
                    namespace_name, tenant_name))
        pulsar_admin_cmd_str = "{} namespaces create {}/{}".format(pulsar_admin_bin, tenant_name, namespace_name)
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str, False)
    else:
        logger.info("   >> Pulsar namespace \"{}\" already exists under tenant \"{}\".".format(
            namespace_name, tenant_name))

    logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR + "\n")
    cmd_output_cnt = cmd_output_cnt + 1

    ###
    # Create a partitioned Pulsar topic if needed. Regular non-partition topic
    #   doesn't need to be created
    if partitioned and _num_partitions > 1:
        logger.info("{}. Create a partitioned topic - number of partitions: {}; topic name: {}".format(
            cmd_output_cnt, _num_partitions, real_topic_name))

        pulsar_admin_subcmd_str = "topics create-partitioned-topic -p {} {}".format(
            _num_partitions, real_topic_name)
        pulsar_admin_cmd_str = "{} {}".format(pulsar_admin_bin, pulsar_admin_subcmd_str)
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str)

        logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR + "\n")
        cmd_output_cnt = cmd_output_cnt + 1

    ###
    # Set Pulsar persistence settings if requested
    if persistence_enabled:
        ensemble_size = int(pfb_persistence_settings['ensembleSize'])
        write_quorum = int(pfb_persistence_settings['writeQuorum'])
        ack_quorum = int(pfb_persistence_settings['ackQuorum'])
        dedup_enabled = pfb_persistence_settings['deduplicationEnabled']

        # Set Pulsar persistence related settings
        logger.info("{}. Set persistence policies - \"tenant/namespace\": {}/{}, policy: {},{},{}".format(
            cmd_output_cnt,
            tenant_name,
            namespace_name,
            ensemble_size,
            write_quorum,
            ack_quorum))

        pulsar_admin_subcmd_str = "namespaces set-persistence {}/{} -e {} -w {} -a {} -r 0".format(
            tenant_name,
            namespace_name,
            ensemble_size,
            write_quorum,
            ack_quorum)
        pulsar_admin_cmd_str = "{} {}".format(pulsar_admin_bin, pulsar_admin_subcmd_str)
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str)

        logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR + "\n")
        cmd_output_cnt = cmd_output_cnt + 1

        logger.info("{}. {} message deduplication".format(
            cmd_output_cnt, ("Enable" if dedup_enabled else "Disable")))

        if dedup_enabled:
            pulsar_admin_subcmd_str = "namespaces set-deduplication {}/{} -e".format(
                tenant_name,
                namespace_name)
        else:
            pulsar_admin_subcmd_str = "namespaces set-deduplication {}/{} -d".format(
                tenant_name,
                namespace_name)
        pulsar_admin_cmd_str = "{} {}".format(pulsar_admin_bin, pulsar_admin_subcmd_str)
        _exec_pulsar_adm_cmd(pulsar_admin_cmd_str)

        logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR + "\n")
        cmd_output_cnt = cmd_output_cnt + 1

    ###
    # Process pulsar-perf related settings
    pperf_common_settings = config_data['pulsar-perf-common']
    pperf_producer_settings = config_data['pulsar-perf-producer']
    pperf_consumer_settings = config_data['pulsar-perf-consumer']

    _combined_settings = dict(pperf_common_settings)
    if client_type == "producer":
        # combined producer settings
        _combined_settings.update(pperf_producer_settings)
        pperf_subcmd = "produce"
    else:
        # combine consumer settings
        _combined_settings.update(pperf_consumer_settings)
        pperf_subcmd = "consume"

    # Make sure "stats-interval-seconds" setting is always set,
    # even if it is not explicitly set in the yaml file
    # --------------------------------
    # stats_interval_keystr = "stats-interval-seconds"
    # if not stats_interval_keystr in _combined_settings:
    #     _combined_settings[stats_interval_keystr] = 10

    pperfCmdOptionStr = _gen_pulsar_perf_cmdopt_str(_combined_settings)
    if duration_in_sec > 0:
        pperfCmdOptionStr = "--test-duration {}".format(duration_in_sec) + " " + pperfCmdOptionStr

    pulsar_perf_cmd_str = "{} {} {} {}".format(
        pulsar_perf_bin,
        pperf_subcmd,
        pperfCmdOptionStr,
        real_topic_name
    )

    # pperf benchmark execution name
    pperf_exec_name = "pperf_bench_" + pperf_subcmd + "_" + _get_dttm_str_utc(_DTTM_FMT2)
    # CSV file for raw metrics output from "pulsar-perf"
    raw_metrics_file_name = "metrics/" + pperf_exec_name + "_metrics.raw.csv"
    # CSV file for "graphite-nized" metrics (in Graphite PlanText Protocol format)
    graphite_metrics_file_name = "metrics/" + pperf_exec_name + "_metrics.graphite.csv"

    raw_metrics_file = None
    graphite_metrics_file = None

    try:
        logger.info("{}. Run Pulsar Perf benchmark: \"pulsar-perf {} {} {}\"".format(
            cmd_output_cnt, pperf_subcmd, pperfCmdOptionStr, real_topic_name))
        cmd_output_cnt = cmd_output_cnt + 1

        logger.info("                     log file: {}".format(log_file_name))
        logger.info("             raw metrics file: {}".format(raw_metrics_file_name))
        logger.info("        graphite metrics file: {}".format(graphite_metrics_file_name))
        if prom_graphite_port is not None and prom_graphite_port != "":
            logger.info("     graphite exporter port: {}".format(prom_graphite_port))
        logger.info(_PULSAR_CMD_OUTPUT_SEPERATOR)
        print("   Pulsar-perf execution is in progress. Please check the output log file for more details ...\n")

        start_time = datetime.now()

        raw_metrics_file = open(raw_metrics_file_name, 'w')
        graphite_metrics_file = open(graphite_metrics_file_name, 'w')
        # graphite_metrics_prefix = "pperf_bench_" + pperf_subcmd
        graphite_metrics_prefix = "ppfb"

        # Execute "pulsar-perf" command
        #   NOTE: "pulsar-perf consume" doesn't respect "--test-duration" parameter
        _exec_pulsar_perf_cmd(
            duration_in_sec,
            pulsar_perf_cmd_str,
            pperf_subcmd,
            raw_metrics_file,
            graphite_metrics_file,
            prom_graphite_port,
            graphite_metrics_prefix
        )

        _process_hgrm_result_file(pulsar_bin_homedir, pperf_exec_name)

        end_time = datetime.now()
        time_diff = end_time - start_time
        logger.info("Pulsar-perf execution time: {} seconds".format(time_diff.total_seconds()))

    finally:
        if graphite_metrics_file is not None:
            graphite_metrics_file.close()

        if raw_metrics_file is not None:
            raw_metrics_file.close()
