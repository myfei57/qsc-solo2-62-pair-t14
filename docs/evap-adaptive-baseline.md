# 蒸发冷自适应基线 — 设计决策记录

日期：2026-10-06
状态：已拍板，已实现（`coldstore/evap/`）

## 一、要解决的问题

旧基线靠人工拍一个值：夏冬环境温度差很大，一个固定值两头都不准；
高频采样的毛刺还会把基线带偏。需要一套随环境温度自适应、抗噪声、
可审计、超限能自动处置的基线。

## 二、两个拍板项

### 决策 1：基线按**日**滑动（24 小时），不按小时

| 选项 | 结论 | 理由 |
|------|------|------|
| 按小时 | 否决 | 只装得下短时天气波动，会把昼夜温差误学进基线；一天内频繁重拟合，基线本身抖动 |
| **按日（24h）** | **采纳** | 覆盖完整昼夜周期，刻画的是"基线随季节/环境温度漂移"这一真实信号；日级窗口对采样噪声天然平滑 |

计算开销控制（这是"按日"能落地的前提）：

- 样本按小时分进 **24 个桶**，每桶只存聚合量（n、∑x、∑y、∑x²、∑xy），不存原始采样；
- 进一个样本：限流 O(1) + 单桶累加 O(1) + 重算回归 O(24)，**与当天采样总量无关**；
- 窗口滑动是惰性的：样本落桶时按绝对小时戳丢弃 age ≥ 24h 的桶，无后台任务、无全量重扫。

### 决策 2：超限**自动切备用风机**，不加大风机

| 选项 | 结论 | 理由 |
|------|------|------|
| 加大风机 | 否决 | 蒸发冷风机是定速机（现场只有 start/stop/切换），没有变频/加机的执行机构，"加大"无法落地 |
| **切备用** | **采纳** | 备用切换链路已存在（`FanSet.healthy_standby()` + `switch_over()`）；主值班风机能力衰减或故障时，换上健康备用是直接有效的处置 |

防毛刺：温度连续超过 **基线 + 死区(1°C)** 达 **2 个采样确认**才切，单次超限只记 verdict 不动作。

## 三、基线模型

```
baseline(ambient) = anchor + slope * (ambient - 25°C)
```

- `slope`、`anchor`：对过去 24h"健康工况"样本做最小二乘线性回归得到；
- `anchor` = 参考环境温度 25°C 处的基线值，斜率 clamp 到 [0, 1.0]（温度越高基线只应越高）；
- 冷启动（不足 24 个样本、未覆盖 6 个以上不同小时桶）：沿用人工种子锚点（默认 32°C），
  且此阶段不叠加死区，避免种子值偏差把冷启动样本冻在门外；
- 学习门控：**温度高于基线(+死区)的样本不进回归**——故障不会把基线训练到故障值本身；
- 人工 `set_baseline`：清空当日聚合重新学习，防止旧数据与操作员新值混合。

## 四、高频采样限流

- 基线学习流最多 **每 30 秒接受 1 个样本**（`MIN_SAMPLE_INTERVAL`），突发流量被丢弃在学习流之外；
- 最新温度始终落盘用于监视，限流只作用于"进基线"这一条路径；
- 判定确认（streak）与限流解耦：每条喂入的观察都参与超限确认，
  保证真正的连续超限不会因为限流而永远无法确认。

## 五、超限自动处置状态机

```
正常样 ──> 高于基线+死区 ──> streak=1，记 evap_verdict(high)，不动作
                          ──> streak=2（确认）
                                ├─ 有健康备用：停机旧值班 → 备用提升为主值班并启动
                                │                记 evap_duty(auto_switch, from, to)
                                └─ 无健康备用：记 evap_duty(switch_unavailable)
                          一次超限事件最多切换一次（latch）
温度回到死区内 ──> 记 evap_verdict(normal)，清零 streak 与 latch
```

## 六、统一口径（遥测 / 报表 / 审计）

所有消费者读同一个 `Condenser.state()` / `effective_baseline()`，不存在第二套算法：

- **遥测**（`GET /telemetry`，CSV 导出同源）：
  `evap_temperature, evap_ambient, evap_baseline, evap_above, evap_status,
  evap_duty, evap_samples, evap_learned`
- **对外报表**（`GET /report`）：直接渲染上述遥测字段；
- **审计清单**（`GET /audit`、`/export/audit.csv`）记录三类结构化 JSON：
  - `evap_baseline`：基线变化（人工种子 / 日回归漂移 ≥0.5°C 才记一条，避免刷屏）；
  - `evap_verdict`：normal↔high 状态翻转（温度、基线、连续次数）；
  - `evap_duty`：auto_switch / manual_switch / switch_unavailable（from/to/原因）。

## 七、接口

| 方法 路径 | 用途 |
|-----------|------|
| `POST /evap/observe` | 高频喂入 `{temperature, ambient, at?}`：限流学习 + 判定 + 自动切备用，返回完整状态 |
| `GET /evap` | 当前状态（温度/环境/基线/anchor/slope/status/samples/duty） |
| `POST /evap/baseline` | 人工种子基线，清空当日聚合重学 |
| `POST /evap/temp` | 仅记录温度（旧接口，不参与自适应学习） |
| `POST /evap/switch` | 人工切换值班风机（记 manual_switch） |

## 八、关键参数

| 参数 | 值 | 含义 |
|------|----|------|
| `BUCKET_COUNT` | 24 | 日窗口小时桶 |
| `MIN_SAMPLE_INTERVAL` | 30 s | 进基线的最小采样间隔 |
| `MIN_SAMPLES / MIN_BUCKETS` | 24 / 6 | 允许首次回归拟合的数据门槛 |
| `DEAD_BAND` | 1.0 °C | 判定死区 |
| `CONFIRM_SAMPLES` | 2 | 超限确认采样数 |
| `MAX_SLOPE` | 1.0 | 温度敏感度上限 |
| `DEFAULT_REFERENCE` | 25 °C | anchor 参考环境温度 |
| `AUDIT_DELTA` | 0.5 °C | 基线漂移落审计的最小变化 |
