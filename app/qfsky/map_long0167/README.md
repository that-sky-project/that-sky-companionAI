# 当前地图长连协议包

本包无设备、HTTP、socket 或账号发现依赖；现有 `qfsky.long0167` Home 模块保持不变。

## 加载同一捕获

```python
from qfsky.map_long0167 import prepare_current_capture, Engine, PreparationError

prepared = prepare_current_capture(
    capture_root, record_dir,
    expected_account_uuid=authenticated_uuid,
    expected_scene=scene,
    expected_resource_revision=revision,
    expected_level_id=level_id,
    expected_netversion=netversion6,
    apk_build=421589,
)
# Join 使用 prepared.profile；Engine 使用 prepared.inputs，禁止分别取两个旧缓存。
```

`record_dir` 是显式单层私有目录名。新捕获保留 `sky-current-scene-actor-pair/schema1`，必须带 `capture_variant=current-profile-player-pair-v1`、同次双读的 `player_fields`（spawn16B / matrix64B / level_flag），两项 join-player proof checks 以及匹配的完整 scene_observation。旧 r4c 记录可由 `load_current_capture` 只读加载 Actor 作离线验证，但 `prepare` 会拒绝缺少 Join 字段；不会借旧 Home 字段或填 identity。

固定失败码经 `PreparationError.code` 返回。`map_capture_join_fields_missing` 是旧记录缺项；`map_capture_join_fields_unverified` 是未完成同捕获双读证明；IO错误归一化为 `map_capture_io_error`，不含路径。`prepared.metadata()` 仅布尔/计数，表示本地输入验证，不是在线或服务端验收。

## 引擎与同图 Move

```python
engine = Engine(account_uuid=authenticated_uuid, enter_game_body=verified_enter_body,
    generation=registration_generation, now=monotonic_after_registration_ack,
    inputs=prepared.inputs, scene=scene, resource_revision=revision,
    level_id=level_id, netversion=netversion6, live_check=actual_lifetime_callback,
    local_idle=previous_local_idle_or_none)
```

`tick(now)` / `handle(outer_type, body, now)` 返回 body-only `FrameGroup` 或 None；外层按原始顺序封装共享计数器、加密并整组发送，全部成功才 `commit(group,sent_count)`。partial/未知发送关闭，不重放。

同地图 server Move 可在最后完整 commit 后 `export_local_idle()`，新 peer 创建实例时传 `local_idle`。只保留本人的原始 Actor 相位起点及累计 prepared revision。账号、场景、地图、build/native、资源/NV、原 cache/Actor 任一变化都拒绝。旧 roster、authority、Scene ACK、Actor saved sequence 不继承；新注册仍等待91ms，然后先当前累计 Actor、当次 Scene/controller、再+1扩展，后续最小120ms。总Actor origin窗口保持1200s，Move不会刷新这个上限。

跨图必须加载目标图新的当前输入，`local_idle=None`；不能改 map ID 或套 Home。带 timed event Actor 的持续重放尚无事件生命周期策略，当前 Engine gate拒绝；不声称完整native Actor/Scene producer。

## 包自身测试

`python <server>/qfsky/map_long0167/tests/run_suite.py --output <counts.json>`

测试导入本生产包；合成数据/假内存不等于真实游戏验证。真实输入 exercise 可调用 `qfsky.map_long0167.exercise_current_inputs.exercise_current_inputs(inputs,uuid)`，其 Enter 明确合成、无网络、不等于实际入服或180秒在线。
