# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件；`batch`：送检批次。

## 送检链路（batch）

批次在建批时登记冷链箱（`box_id`）、实验室接收时段（`lab_slot`）、保存期限（`storage_deadline`）和箱位容量（`capacity`），状态机为 `assembling → dispatched → received → closed`，退回时回到 `assembling`（待入批）。

- `add_sample`（inspector/admin）：样本须已采集（`collected`）且未过保存期才能入批；保存期限取样本自身 `storage_deadline`（如有）与批次保存期限中较早者。同一样本不能同时挂在两个未完成批次上；并发入批由数据库事务保证只有一个成功。箱位已满时样本进入 `queued_sample_ids` 排队。
- `dispatch` / `receive`：发运与实验室签收，箱内样本状态随批次联动（`batched → in_transit → received`）。
- `lab_return` / `cold_chain_breach`（需 `reason`，可选 `occurred_at`）：整批退回待入批；保存期已过的样本就地作废（`voided`）并写入作废原因，空出的箱位按排队顺序补位。已回传结果的批次禁止退回。
- `report_result`（lab/admin，需 `sample_id`、`result`、`result_id`、`seq`）：实验室分批回传结果。`result_id` 幂等去重，重复回传不产生任何变更；`seq` 小于等于已应用序号的结果记为乱序，写入 `ignored_results` 而不生效。结论发生变更时，样本结论按新结果更新，但相关案件不会被悄悄改写——案件只被标记 `needs_reconfirmation`，已生效的裁决保持原样。
- `close`：关闭批次，仍挂在批上的样本释放回 `collected`，可重新入批。

案件在结论变更后需通过 `reconfirm`（panel/admin，`outcome` 为 `upheld` 或 `overturned`）重新确认：`upheld` 维持原状态，`overturned` 将案件置为 `dismissed`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
