# 自动版本标签验证

基线：`7f325d95d43db7ba01e994ab20f7aca80b19b921`，应用版本 0.4.3。
本地版本保持不变，主工作区未提交的其他任务修改未纳入本分支。

## 行为与调用链

主线 push → Version 自动递增并记录 revision → Backend / Frontend 检出该 revision
并通过质量门 → Tag 为该 revision 创建 `v<应用版本>` → 成功 CI 触发既有 Docker
构建，生成同一 revision 的 latest、SHA 和版本镜像标签。

PR 和功能分支不创建 Git 标签。质量门失败不创建标签；已有同名同提交标签复用，
同名不同提交标签停止并保持原样。脚本验证当前检出提交、远程 main 归属、版本格式
及包元数据一致性，使用显式 SHA 推送，不强制推送、不移动主线。

自动标签使用 `GITHUB_TOKEN`，不会触发另一轮 tag push 构建，所以 Docker
workflow_run 路径同步增加版本镜像标签。桌面 Release 和部署仍按现有流程验收。

## 本地实际验证

实现前新增标签和工作流用例：19 failed，原因为缺失标签实现和 Tag Job。
实现后以下适用回归：**69 passed，41.44 秒**。

```bash
PYTHONPATH=backend:scripts backend/.venv/bin/python -m pytest \
  scripts/test_tag_version.py scripts/test_version_workflows.py \
  scripts/test_advance_version.py scripts/test_versioning.py \
  backend/tests/test_version_source.py -q
```

标签用例通过临时 bare Git 仓库实际验证创建、annotated tag 复用、同名冲突、
未推送提交拒绝、精确检出、非法事件和 SHA、脏目录保护、并发同名发布、权限拒绝、
版本一致性和 CLI 输出。此前一次使用主工作区解释器但未指定本工作区 PYTHONPATH，
版本来源测试误读旧工作区应用；纠正环境后上述回归全部通过。

- Ruff：`scripts/tag_version.py`、`test_tag_version.py`、`test_version_workflows.py`
  全部通过。
- actionlint：`ci.yml` 和 `docker.yml` 通过。
- `scripts/check_version.py --current VERSION` 通过，仍为 0.4.3。
- `git diff --check` 通过。

## 兼容与交付边界

复用现有版本工具、CI revision 和镜像流程，不改应用业务或用户数据。
贡献规范、测试指南、发布操作和版本基线同步说明自动标签与稳定 Release 的边界。
未发现本地阻断问题；远程标签创建权限和完整 CI 结果以实际主线运行记录为准，
本报告不预先声称远程写入成功或镜像/桌面/生产已验收。

回滚时撤回工作流和标签脚本变更即可停止未来自动标签；已经创建的不可变标签保留。
