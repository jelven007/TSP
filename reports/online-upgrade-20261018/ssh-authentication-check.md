# ECS SSH 认证核验

2026-10-18 10:15，北京时间。目标 `root@118.196.108.188:22`。

用户已提供 root 登录凭据。SSH 服务可达，但服务端声明唯一可用认证方式为 `publickey`；没有提供 `password` 或 `keyboard-interactive` 认证，因此未进行密码校验。

实际响应：

```text
Remote protocol version 2.0, remote software version OpenSSH_9.6p1 Ubuntu-3ubuntu13.8
Authentications that can continue: publickey
root@118.196.108.188: Permission denied (publickey).
```

当前机器没有目标主机的部署私钥，SSH agent 也没有可用身份。首次接入记录了主机公钥，后续复核使用该记录；尚未获得云控制台或原可信记录的独立主机身份校验。

本次没有进入服务器，没有执行备份、数据库迁移、镜像更新或路由切换。用户密码没有写入文件、命令行或发布记录。待提供原部署私钥路径，或在 ECS 控制台恢复可用的 SSH 登录后，继续此前已授权的升级。

本地持续验收任务仍运行，最近核验为 100 活跃用户、100 条 SSE，零隔离违规和私有探针超时。

## 后续状态

同日稍后通过火山引擎 ECS 临时 ED25519 公钥恢复了固定指纹的 SSH 访问，并完成
v0.4.8 升级。最终核验后已从 `root` 的 `authorized_keys` 精确删除该临时公钥；
等待 5 秒未重新注入，使用原私钥复测登录失败，本地临时密钥材料也已删除。完整结果见
[deployment-result.md](./deployment-result.md)。
