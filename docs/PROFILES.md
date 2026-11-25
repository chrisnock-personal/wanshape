# Built-in profiles

`wanshape list` shows them and `wanshape show <name>` prints every setting. Delays are **one-way and apply in each direction**, so the round trip gains twice the delay. Values are starting points for testing, not measurements of any particular network.

## What they do

Measured with `tools/measure-profiles.sh`: an RTP-like UDP stream (50 packets a second for 20 s) echoed across a veth pair in a private namespace, on Fedora's 7.3 kernel, 2026-11-25. "Lost" is the share of probes that didn't come back, so it counts loss in both directions. Bursty loss varies a lot over 20 s, so profiles with a loss model were measured three times and show the range.

| Profile | One-way delay / jitter | Loss each way | Down / up | Round trip, median (p95) | Lost, expected | Lost, measured |
|---|---|---|---|---|---|---|
| `office` | 5 ms / 1 ms | 0 | 100 / 100 mbit | 10.8 ms (13.1) | 0 | 0.0% |
| `dsl` | 15 ms / 2 ms | 0 | 16 / 1 mbit | 31.0 ms (35.5) | 0 | 0.0% |
| `leo-satellite` | 22 ms / 8 ms | bursty, ~0.5% | 100 / 15 mbit | 45.6 ms (63.7) | ~1% | 0.0-3.0% |
| `4g` | 25 ms / 10 ms | 0.1% | 10 / 5 mbit | 52.6 ms (74.3) | 0.2% | 0.0% |
| `transatlantic` | 32 ms / 1 ms | 0 | unlimited | 64.7 ms (67.1) | 0 | 0.0% |
| `wifi-roaming` | 10 ms / 10 ms pareto, correlated | bursty, ~3.2% | 20 / 10 mbit | 17.9 ms (45.2) | ~6.3% | 3.6-8.6% |
| `lte-congested` | 40 ms / 20 ms, released in 5-15 ms slots | bursty, ~1% | 5 / 1 mbit | 106.9 ms (146.8) | ~2% | 2.5% |
| `lossy-sip` | 50 ms / 15 ms | bursty, ~3.8% | 512 / 512 kbit | 106.7 ms (138.3) | ~7.5% | 11.4% |
| `bad-wifi` | 40 ms / 60 ms pareto | 5% | 2 / 1 mbit | 157.9 ms (332.1) | 9.8% | 10.8% |
| `voip-poor` | 100 ms / 30 ms | bursty, ~2% | unlimited | 199.9 ms (274.0) | ~3.9% | 2.3-3.7% |
| `3g` | 150 ms / 30 ms | 1% | 1600 / 768 kbit | 328.8 ms (379.9) | 2.0% | 2.3% |
| `voip-bad` | 200 ms / 60 ms | bursty, ~5.7% | unlimited | 399.3 ms (542.4) | ~11% | 12.9% |
| `2g-edge` | 200 ms / 50 ms | 1% | 200 / 100 kbit | 473.2 ms (551.5) | 2.0% | 2.2% |
| `satellite` | 300 ms / 20 ms | 0.5% | 5 / 1 mbit | 611.1 ms (653.1) | 1.0% | 0.4% |

"Bursty" profiles use netem's Gilbert-Elliott model (`loss_model = "gemodel P R 100% 0%"`): every packet in the bad state is lost, and the average loss is P / (P + R) with bursts of about 1 / R packets. `lte-congested`'s extra delay above 2 x 40 ms comes from slotting: packets wait for the next slot.

## Where the numbers come from

| Profile | Basis |
|---|---|
| `leo-satellite` | Starlink median latency about 45 ms round trip (Ookla, Q1 2025, [Light Reading](https://www.lightreading.com/satellite/starlink-smokes-geo-satellite-operators-in-speed-latency-report)); Starlink's own peak-hour median 25.7 ms ([Starlink](https://starlink.com/updates/network-update)). Loss bursts stand for satellite handovers. |
| `transatlantic` | London-New York about 63.5 ms round trip on a modern route, 54.7 ms physical floor ([Hats Network](https://hatsnet.io/docs/network/latency/pairs/lon-nyc-rtt)). |
| `2g-edge` | Observed EDGE downlink about 100-250 kbit/s ([US DoJ](https://www.justice.gov/atr/wireless-broadband-description-mobile-wireless-data-observed-performances-gsm-technology-family)); 2G round trips in the high hundreds of milliseconds ([High Performance Browser Networking](https://hpbn.co/mobile-networks/)). |
| `voip-poor`, `voip-bad` | ITU-T G.114: below 150 ms one-way most applications aren't significantly affected; 400 ms is the planning limit ([ITU-T G.114](https://www.itu.int/rec/T-REC-G.114/index.html)). `voip-poor` stays under 150 ms one-way, `voip-bad` goes past it. |
| `office`, `4g`, `3g`, `satellite`, `bad-wifi`, `lossy-sip` | The original set: typical values from common experience, to be tuned in use. |
| `dsl`, `wifi-roaming`, `lte-congested` | Illustrative, not from a single source: typical ADSL2+ figures, Wi-Fi roaming as bursty loss with correlated delay, and LTE scheduling as slotting. |

## Not supported: simple loss correlation

netem accepts `loss P CORRELATION`, but on the kernels tested it drops far fewer packets than asked: `loss 3%` dropped 2.8% of 20,000 packets, `loss 3% 25%` dropped 0.03%, and `loss 3% 75%` dropped none. wanshape therefore doesn't offer it. For bursty loss, use `loss_model = "gemodel P R 100% 0%"`, which measured as expected.
