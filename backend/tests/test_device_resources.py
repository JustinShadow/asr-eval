"""设备硬件资源采样解析测试。"""

from app.domain.device_resources import parse_resource_output


def test_parse_resource_output() -> None:
    output = """##MEM
MemTotal:       16000000 kB
MemAvailable:    8000000 kB
##LOAD
0.50 0.40 0.30 1/100 1234
##CPU1
cpu  100 0 100 800 0 0 0 0 0 0
##CPU2
cpu  150 0 150 900 0 0 0 0 0 0
##NPU
NPU load:  Core0:  40%, Core1:  60%,
##GPU
50@900000000Hz
##THERMAL
45000
50000
"""
    result = parse_resource_output(output)

    assert result["cpu_pct"] == 50.0
    assert result["mem_total_mb"] == 15625
    assert result["mem_used_pct"] == 50.0
    assert result["npu_pct"] == 50.0
    assert result["gpu_pct"] == 50.0
    assert result["gpu_freq_mhz"] == 900
    assert result["temp_c_max"] == 50.0
    assert result["load1"] == 0.5
