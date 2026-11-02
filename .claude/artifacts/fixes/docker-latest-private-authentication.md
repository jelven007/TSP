# Bug: 私有仓库的 latest 镜像发布缺少主线读取认证

> Status: FIXED
> Mode: quick
> Last updated: 2026-11-02

## 根因和修复

主线CI对应的双平台镜像已经构建，`promote-latest`检出使用
`persist-credentials: false`，随后`is_current_main`调用私有origin的
`git ls-remote`时没有认证，工作流因此失败。不是版本镜像构建失败。
该步骤使用GITHUB_TOKEN及进程环境中的Git extraheader完成只读主线检查，
不将凭据写入Git配置。仍只有固定构建SHA等于当前main时才提升latest。

## 验证

`test_private_repository_main_check_receives_ephemeral_authentication`修复前失败，
修复后实际执行工作流Shell/Python，模拟远程读取确认认证环境存在；
过期SHA保持latest，凭据不出现在标准输出或异常输出。
线上真实工作流结果以本轮新主线CI后的Docker回执为准。
