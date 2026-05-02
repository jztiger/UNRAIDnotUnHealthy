#!/usr/bin/awk -f
# Read /proc/net/dev format and emit Prometheus textfile-format counters
# for host-namespace network throughput. Used by the host_netdev_textfile
# s6 service.
BEGIN {
  print "# HELP node_network_host_receive_bytes_total Host-namespace network receive bytes."
  print "# TYPE node_network_host_receive_bytes_total counter"
  print "# HELP node_network_host_transmit_bytes_total Host-namespace network transmit bytes."
  print "# TYPE node_network_host_transmit_bytes_total counter"
}
NR > 2 {
  iface = $1
  sub(/:$/, "", iface)
  printf "node_network_host_receive_bytes_total{device=\"%s\"} %s\n",  iface, $2
  printf "node_network_host_transmit_bytes_total{device=\"%s\"} %s\n", iface, $10
}
