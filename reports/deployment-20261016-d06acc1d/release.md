# TSP d06acc1d 生产发布记录

## 发布结果

- 状态：成功
- 生产版本：`d06acc1d8fe27f263f8e7d63fe5093b03e62ae9f`
- 切流时间：2026-10-16 13:31:50 至 13:32:16（UTC+8）
- 生产地址：<https://118.196.108.188/>
- 当前 release：`/opt/tsp-full/release-d06acc1d`
- 前一版本：`/opt/tsp-full/release-8f3afbf`

本次只发布独立密码找回提交 `d06acc1d`，未包含其后的实时网关和二期代码。
登录页新增“忘记密码”，找回流程校验邮箱、注册口令、专用邮箱验证码、新密码及确认
密码。验证码有效期 10 分钟、最多尝试 5 次；重置成功后撤销该用户全部会话和 API
Token。未知邮箱使用统一响应，注册码与找回码相互隔离。

本次无数据库 schema 迁移，生产数据库保持 schema v5。

## 镜像

- Runtime：`sha256:c28c8b57647430b6d9d430769ce5d18446f2bd04a7af52367e83c98c257b70fd`
- Compute：`sha256:a58fee6af57a34b908648b44df30ee91c217fa4fd93e5a27e8dc81fa26120d94`
- 六个常驻业务容器已统一使用新 Runtime 镜像。

## 备份与恢复验证

生产备份位于：

`/opt/tsp-full/backups/pre-d06acc1d-20261016-133142`

备份约 598 MB，包含旧镜像、旧 Compose、平台配置、Broker、Nginx/TLS、快照、
双库 dump、容器基线和 release 指针。

- control 库：schema v5，20 张表，隔离恢复成功
- data 库：schema v5，34 张表，隔离恢复成功
- 临时恢复数据库已清理，残留数量为 0
- 回滚脚本权限：`0700`

回滚命令：

```bash
python3 /opt/tsp-full/backups/pre-d06acc1d-20261016-133142/rollback.py \
  --rollback /opt/tsp-full/backups/pre-d06acc1d-20261016-133142
```

## 验收结果

- 精确提交回归：后端 34 项、前端 5 项全部通过
- 前端生产构建、Ruff、`git diff --check` 通过
- 隔离候选：深度健康 9/9，密码找回 OpenAPI 与 Auth 静态包校验通过
- 公网 HTTPS：本地 CA、证书主机名和真实公网链路验证通过
- 生产 API：`/health/ready` 与 `/api/health` 均为 9/9
- 连续稳定性：3 轮深度健康全部通过
- 运行状态：六个容器重启次数均为 0，Broker 为 active
- 页面验收：可进入和退出“找回密码”，五个输入项及发送/重置控件完整
- 浏览器控制台无错误
- 安全烟测：带正确 Origin 的空请求返回 HTTP 422，未实际重置生产用户
- 发布后 API、Platform 和 Broker 日志未发现 error、exception、fatal 或 traceback

详细证据见同目录 JSON 文件。
