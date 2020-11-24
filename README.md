- [1. Overview](#1-overview)
  - [1.1. Properly Capture Pulsar Perf Execution Metrics](#11-properly-capture-pulsar-perf-execution-metrics)
  - [1.2. Fine Tune Pulsar Cluster Behavior](#12-fine-tune-pulsar-cluster-behavior)
- [2. Benchmark Utility Description](#2-benchmark-utility-description)
  - [2.1. Configuration File](#21-configuration-file)
    - [2.1.1. Limitation](#211-limitation)
  - [2.2. Execution Output](#22-execution-output)
    - [2.3. Metrics Integration with Prometheus and Grafana](#23-metrics-integration-with-prometheus-and-grafana)

# 1. Overview

[Pulsar perf](https://pulsar.apache.org/docs/en/performance-pulsar-perf/) is Apache Pulsar's built-in load generation and performance testing tool. It is a powerful tool and has fairly comprehensive options to simulate and test various aspects of a Pulsar workload, such as Producer, Consumer, Reader, and etc. 

When executing a Pulsar perf command, it outputs the end-to-end performance metrics (throughput and latency) directly on the command line, something like below. When the execution finishes, it generates a single HdrHistogram result file. 

```
...
09:49:44.420 [main] INFO  org.apache.pulsar.testclient.PerformanceProducer - Throughput produced:  28907.7  msg/s ---     22.1 Mbit/s --- failure      0.0 msg/s --- Latency: mean:   0.000 ms - med:   0.000 - 95pct:   0.000 - 99pct:   0.000 - 99.9pct:   0.000 - 99.99pct:   0.000 - Max:   0.000
09:49:54.502 [main] INFO  org.apache.pulsar.testclient.PerformanceProducer - Throughput produced:  42617.5  msg/s ---     32.5 Mbit/s --- failure      0.0 msg/s --- Latency: mean:  84.185 ms - med:  79.618 - 95pct: 124.642 - 99pct: 154.895 - 99.9pct: 240.600 - 99.99pct: 241.294 - Max: 257.830
09:50:04.566 [main] INFO  org.apache.pulsar.testclient.PerformanceProducer - Throughput produced:  27091.7  msg/s ---     20.7 Mbit/s --- failure      0.0 msg/s --- Latency: mean: 125.973 ms - med:  79.933 - 95pct: 295.433 - 99pct: 966.467 - 99.9pct: 1558.535 - 99.99pct: 1833.951 - Max: 2000.367

...
``` 

## 1.1. Properly Capture Pulsar Perf Execution Metrics

The way that Pulsar perf captures the end-to-end performance metrics, in my opinion, is rather primitive. It doesn't allow the metrics to be exported to an external file (e.g. a CSV file) or to be integrated with a graph/dashboard system like Prometheus and Grafana.

The **main objective** of this repo. is to provide a wrapper utility around Pulsar perf so the end-to-end- performance metrics can be properly captured:
* The throughput and latency metrics will be saved in a CSV file.
* If a remote Prometheus Graphite exporter listening host and port (e.g. *<host_ip>:9019*) is provided, the metrics will also be sent to that host and port in [PlainText Protocol](https://graphite.readthedocs.io/en/stable/feeding-carbon.html#the-plaintext-protocol) format. By doing so, the Pulsar perf execution metrics can be integrated into Prometheus and Grafana.

## 1.2. Fine Tune Pulsar Cluster Behavior

Another limitation of Pulsar perf utility is that it doesn't offer the capability to fine tune some key cluster parameters that are critical for the overall performance. Some of these key parameters are:
* Whether or not a topic is partitioned (by default, Pulsar perf will create a non-partitioned topic and run test against it).
* How the bookie persistence behavior is configured, e.g.
  * ensembleSize
  * writeQuorum
  * ackQuorum
* Whether or not message deduplication is allowed

The capability of being able to fine tune these cluster-specific settings, in an automatic way, is often crucial in a performance benchmark testing. Pulsar perf, however, doesn't have this capability out of the box. 

The **secondary objective** of this repo is to extend Pulsar perf's capability in this area and therefore become a repeatable benchmark testing platform.

# 2. Benchmark Utility Description

The utility in this repo, **pperf_bench.py**, is Python based and is tested with Python version 3.8.6.

The utility takes several command-line arguments, as listed below:
```
usage: pperf_bench.py [-h] [-f [CONFIG]] [-d [DURATION]] [-t TOPIC]
                      [-g [PROM_GRAPHITE]]

optional arguments:
  -h, --help            show this help message and exit
  -f [CONFIG], --config [CONFIG]
                        benchmark configuration file (default: "ppfb.yaml"
                        file under the same directory).
  -d [DURATION], --duration [DURATION]
                        benchmark execution duration (format:
                        <integer_value>[h|m|s], default: 10m).
  -t TOPIC, --topic TOPIC
                        pulsar topic name (format:
                        "<tenant>/<namespace>/<topic>").
  -g [PROM_GRAPHITE], --prom_graphite [PROM_GRAPHITE]
                        Prometheus graphite exporter host and port (format:
                        <host_ip>:9109
```

Among these arguments, the Pulsar topic name is mandatory.

**NOTE**: when specifying the topic name, please do NOT include "persistent://" (or "non-persistent://") prefix as you would normally do for a Pulsar topic. Instead, the information (persistent or non-persistent) is provided in the configuration file.

## 2.1. Configuration File 

By default, the utility takes the configuration inputs from a file named **ppfb.yaml** file under the same directory. At the moment, the configuration items in this file are grouped under 5 major categories:

* **pfb-general**: General configuration items related with one benchmark testing, such as: 1) if the topic is persistent or non-persistent, 2) if the topic is partitioned, 3) Pulsar perf workload simulation type: producer or consumer, and etc.

* **pfb-persistence**: Configuration items that are specific to Pulsar persistence (bookie), such as: 1) ensemble size, 2) write/ack quorum, 3) whether or not message deduplication is enabled, and etc. 

* **pulsar-perf-common**: Pulsar-perf related configuration items that are common to all client simulation types, such as: 1) message processing (producing/consuming) rate, 2) maximum connections per single broker, 3) message encryption key file, and etc.
  
* **pulsar-perf-producer**: Pulsar-perf configuration items that are specific to a **Producer**, such as: 1) number of producers, message size, message payload file, and etc.

* **pulsar-perf-consumer**: Pulsar-perf configuration items that are specific to a **Consumer**, such as: 1)number of consumers, 2) receiver queue size, 3) subscription type (e.g Exclusive, Shared, ...), and etc.
  
### 2.1.1. Limitation

1. At the moment, this utility **ONLY** supports 2 "*pulsar-perf*" cli commands: **produce** and **consume**. It is planned to expand the capability of this utility to other commands in the future (e.g. read, websocket-producer, managed-ledger, and etc.)  

2. When configuring Pulsar perf related settings (under categories: **pulsar-perf-***), the utility can take any valid "*pulsar-perf [produce|consume]*" cli command line option. **BUT**, the long form of the option MUST be used. The short-form notation is NOT recognized and will cause execution failure. 

   For example, in "*pulsar-perf*" cli, you can use either "-r" or "--rate" to specify the message processing rate. But in this utility, it has to be specified as "--rate".

## 2.2. Execution Output

When executing the utility, it will create a log file (under **logs** sub-directory) to capture detailed execution details and also generate several metrics files (under **metrics** sub-directory) with the following naming convention

| Sub-folder/File Name | Description |
| -------------------- | ----------- |
| logs/pperf_bench_<execution_date_time>.log | main log file |
| metrics/pperf_bench_<execution_date_time>_metrics.raw.csv | raw metrics in tabular CSV format |
| metrics/pperf_bench_<execution_date_time>_metrics.graphite.csv | (**Optional**) Prometheus Graphite Exporter oriented format |
| metrics/pperf_bench_<execution_date_time>.hgrm | (**Optional**) The original HdrHistogram file generated by *pulsar-perf* cli |

### 2.3. Metrics Integration with Prometheus and Grafana

The following screenshot shows an example displaying the bench execution metrics on a Grafana dashboard when the metrics are exposed to a Prometheus server via a Prometheus Graphite exporter.

<img src="https://github.com/yabinmeng/pulsar_perf_bench/tree/master/screenshots>/grafana.png" width="800">
