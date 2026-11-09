# 源码构建与运行 · r8

本仓库基于 `1.0.5-private-preview.8-redemption` 客户端源码，不含私有网易登录实现、账号库、服务密钥、游戏 APK 或网格资源。`BUILD.json` 记录原 r8 发布时的状态，不表示本次重新执行了这些线上测试。

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

高级人设、资源安装与原有功能由同源的完整向导/高级面板承接，桌面宿主不另开一套游戏逻辑。

## 离线检查

在装有 `app/requirements.txt` 依赖的 Python 3.12 环境中运行：

```sh
python -B -m unittest discover -s tests -v
```

测试包括公开/私有边界、Python 源码语法、真实客户端依赖导入与安全验证结果适配；使用临时目录和伪客户端，不访问账号或上游网络。

本次源码替换未重新执行 EXE 构建、系统运行时安装、线上手机号/邮箱登录、SMTP、游戏互动及地图回归。完整发行包曾完成的功能验证与本次源码检查分开记录，不能用离线测试替代这些线上结果。

## 许可与配置边界

保留原仓库 `LICENSE`、`NOTICE`，以及客户端 `THIRD_PARTY_NOTICES`。不要提交运行后生成的账号缓存、日志、抓包、令牌或服务器私有目录。客户端为运行而临时取得的游戏 session 仍是敏感数据。
