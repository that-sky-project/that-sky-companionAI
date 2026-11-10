# 源码构建与运行 · r10

本仓库的 r10 在 r9 完整桌面功能基线上重做前端界面，不含私有网易登录实现、账号库、服务密钥、游戏 APK 或网格资源。`BUILD.json` 分别记录本次验证与继承的 r8 发布记录；旧版线上结果不视为本次重新验证。

## 构建 Windows 窗口宿主

使用 Windows、完整 Python 3.12，在仓库根目录运行：

```powershell
.\tools\build_desktop.ps1 -Python python
```

脚本创建隔离的 `.build-env`，安装 `requirements-desktop.txt` 中的构建依赖，输出 `XiaoGuang.exe`。这一步只构建窗口宿主，不会登录游戏、生成地图或部署服务；它不自动打包便携运行时。

## 组装完整客户端

- 将可信的**同版本 Windows 发行包**里的 `runtime` 放在 `XiaoGuang.exe` 旁；它包含便携 Python、Node 及已安装依赖。不要复制任何旧 `data`、日志、账号或令牌文件。
- 目录必须保留 `XiaoGuang.exe`、`app/`、`runtime/python/python.exe` 和 `runtime/node/node.exe`。窗口宿主固定使用这些相对路径，不会自动改用系统 Python。
- Python 运行依赖列在 `app/requirements.txt`；桌面构建依赖与后台运行依赖是两份清单。不要只安装桌面依赖就尝试启动后台。
- 源码不含 `node_modules`。需要自行准备引擎依赖时，使用 Node/npm 按锁文件运行：

```sh
npm ci --prefix app/engine
npm ci --prefix app/xg_gamedata/terrain/pipeline/engine
```

- Windows 需安装 Microsoft Edge WebView2 Runtime。不要只分发单个 EXE。
- 游戏资源与地形由完整向导从用户自己的 APK 生成；首次使用需注册/登录小光服务，再添加网易游戏账号并配置 AI。服务令牌由登录流程获取，不随源码提供。
- 默认数据位于 `%LOCALAPPDATA%\XiaoGuangPrivate\data`，不在源码目录中。

人设、触发前缀、主人绑定与称呼、AI 配置、日志和记忆、维护、资源安装与录制均可在 EXE 内操作，不再要求跳回原页面。桌面使用原有同源 API，不另起一套游戏逻辑。

前缀为 1～3 个非空 Unicode 字符，不接受控制字符或换行，保存后下一句生效。称呼为 1～6 个汉字、字母或数字，保存前须通过随包 780 条游戏聊天规则及常见句式检查；缺少规则、编译失败或命中时保留旧称呼。该离线检查不是对游戏后续规则变动的保证。称呼不改主人 UUID；聊天运行中修改称呼后，可点“重启聊天生效”，不会重连游戏。

## 离线检查

在装有 `app/requirements.txt` 依赖的 Python 3.12 环境中运行：

```sh
python -B -m unittest discover -s tests -v
```

测试包括公开/私有边界、Python 源码语法、真实客户端依赖导入、安全验证结果适配，以及前缀、称呼审核和自启动选择。桌面设置测试在隔离子进程中使用真实 loopback Handler 与 PCRE2 规则，数据与 Startup 均为临时目录；禁止上游连接及真实子进程启动，不触碰账号或系统自启动。

本次复用已构建的 r9 EXE 宿主（桌面宿主与后台逻辑未改），对重写后的界面进行本机 fixture 回归；具体通过项见 `BUILD.json` 的本次验证记录。线上手机号/邮箱登录、SMTP、游戏互动与地图回归未重新执行；这些继承功能不等于已重新通过线上测试。

完整便携包可在新的临时数据目录运行真实 EXE 后台启动检查，不会登录游戏：

```powershell
.\XiaoGuang.exe --backend-smoke --data-dir "$env:TEMP\xg-r10-smoke" --smoke-report "$env:TEMP\xg-r10-smoke.json"
```

## 许可与配置边界

保留原仓库 `LICENSE`、`NOTICE`，以及客户端 `THIRD_PARTY_NOTICES`。不要提交运行后生成的账号缓存、日志、抓包、令牌或服务器私有目录。客户端为运行而临时取得的游戏 session 仍是敏感数据。
