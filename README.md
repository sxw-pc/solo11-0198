# 散货列车车皮滞留计费采录台

面向散货卸货场值班员、承运人交接员和联运账务复核员。它用本地采录文件裁决哪节车皮何时入场、哪些分钟因场站条件停计、何时返空以及每个联运责任方该承担多少费用。业务规则和样例金额均为项目定义。

## 启动

需要 Python 3.10+（含 `sqlite3`）。在本目录执行，所有金额为整数分，无第三方依赖。

```powershell
python -B -m railcar --db ':memory:' demo
python -B -m unittest discover -s tests -v
```

以 `shift.sqlite` 保留各命令之间的状态。

```powershell
python -B -m railcar --db shift.sqlite init
python -B -m railcar --db shift.sqlite seed
python -B -m railcar --db shift.sqlite custody V-17 --as-of 210
python -B -m railcar --db shift.sqlite operations V-17 --start 100 --end 200
python -B -m railcar --db shift.sqlite liability V-17 --start 100 --end 200
```

以下命令按顺序生成初算、复算与退补。示例编号适用于新建的数据库。

```powershell
python -B -m railcar --db shift.sqlite accrue V-17 --as-of 210
python -B -m railcar --db shift.sqlite invoice fixtures/invoice.json
python -B -m railcar --db shift.sqlite capture fixtures/late-capacity.json
python -B -m railcar --db shift.sqlite accrue V-17 --as-of 210
python -B -m railcar --db shift.sqlite prepare INV-17 2 billing-clerk
python -B -m railcar --db shift.sqlite adjustment 1
python -B -m railcar --db shift.sqlite approve 1 reviewer
python -B -m railcar --db shift.sqlite balance INV-17
python -B -m railcar --db shift.sqlite audit 1
```

样例首版应计 A 为 150 分，B 为 650 分。场站更正将第 120–150 分钟的可用能力从 1 改为 0。停计横跨第 135 分钟的责任交接，修正后只应计 B 的 500 分。原始账单仍显示 A 150、B 650，审批分录分别退 150，旧 run 可重算。`tariff V-17 --as-of 100` 可单独查看第 100 分钟适用的费率。`evidence 14` 可读回支撑该裁决的原始采录版本。

## 采录与裁决

- **录入约定**。时间是相对于 `2026-01-01T00:00:00Z` 的整数分钟。设备 `raw` 是以同一纪元表示的本地读数，只有命中唯一校时凭证才能以 `raw + offset` 转成 UTC 分钟。时间窗均左闭右开，`as-of` 包含该分钟发生的证据事件，计费区间右端不计费。人手录入的检验和签署时间直接使用 UTC 分钟。硬规则的阈值为站界两读数相差最多 10 分钟、联运双方接受时间相差最多 15 分钟。
- **站界与交付**。一辆车的场次窗全站互斥。挂牌映射可随时间复用，校时后的挂牌必须唯一对应场次车。只有合同轨道上同一车的承运方和场方各有一个合格读数、设备独立且校时差满足容限，才按较晚读数建立交付。重复合格读数阻挡，错误轨道和异车读数可在报告中定位。`custody` 无需费率或账单。
- **返空**。从已交付到最早一侧出场读数之间，取最后一分钟的检验结论。冲突和拒绝会阻止返空，合格检查只建立待返空。出场也需双侧站界证据且两侧在交付之后，较晚的出场分钟结清占用。待返空按 `as-of` 和场次窗右端的较早者暂计，一直保持暂计状态。
- **现场作业**。仅设备故障可享停计。场方和承运人在同一中断版本上都签认通过且当前无否决，然后逐段考察全站可用槽位。只有覆盖的证书都为零且有至少一个合格故障才停计。缺证书、容量余量或冲突都保持计时。相交停计只计一次。`operations` 可用显式区间独立查看中断签认和容量裁决。
- **联运归责与计费**。同一转手版本的双方接受后，以较晚签署时间生效，两笔均须在占用区间内。撤回按签署版本作证据更正。未完成转手续由现方负责。同刻两项转手或已接受的出让方与现责任方不符，整份应计冻结。交付时刻唯一命中的费率固定于该次计算。可计分钟依时间顺序跨方消耗一次免计额度，停计分钟不消耗。`liability` 可用显式区间独立看归责。
- **核账**。每场次一份不可变原始账单，载入时按责任方提供正分金额。最终应计与原账加已批准退补的余额逐方比较，正差补、负差退。经手人和复核人须是不同的操作员标识。审批用单个写事务同时核验当前证据重算和已入账分录基线。`adjustment` 可查待审、过期和已入账提案。操作员标识依赖本地现场管理。

`capture` 接收严格的 UTF-8 JSON 对象，结构与 `fixtures/station.json` 相同。`batch` 标识整份文件，`events` 是按依赖排列的事件。表态必须引用已存在的 `incident_rev` 或 `handoff_rev`。修改有 `id` 的证据时，沿用该记录 ID 并发送完整记录。原版本仍在。签认只覆盖同业务对象、同被签版本、同角色的旧签认。`capture` 返回首尾版本，同一批次原样重投是幂等重读，复用已提交的批号递交不同内容会被拒绝。

## 审计与故障恢复

采录批内任一记录失败会回滚整个批和版本号。修正错误后可以继续使用未提交的批号。不要更改已提交批号的内容。`--cutoff` 只能指定成功批次的尾版本。SQLite 串行化采录、应计和审批写入，重开同一文件可继续操作。重复记录相同场次、截止版本、应计时点会返回同一 run。`audit RUN` 用原始证据重新计算并比较整份结果，退出码 1 表示不一致，输入或事务错误退出码为 2。

过期退补需按当前证据重新 `accrue`、`prepare`、`approve`。已批准退补留在余额中，后续更正对已入账余额再出退补。原账、应计快照和审批均可追查。暂计和冻结账单不能触发退补。
