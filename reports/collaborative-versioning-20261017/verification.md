# 多人协作版本管理验证

基线：main `21333e616007e061c25f04217f32a9df4b12b1e2` / 0.4.4。
本次不人工增加应用版本；使用独立 worktree，主工作区已有任务修改未纳入。

## 发现及修正

1. PR 原来只检查格式与元数据一致性，所有文件人工同步加号仍可通过。
   增加实际 PR head 与共同 main 祖先的版本检查，允许自然合并 main 更新基线。
2. 主线旧分支或误操作可回退版本；来源 trailer 可被普通提交复用而跳过分配。
   验证自动版本提交的文件集合、精确内容和后继版本；检查最新已分配号不回退，
   自动提交再次触发时复用自身。保留每事件独立计数和现有并发普通推送重试。
3. 桌面发布构建触发时的分支，而输入 tag 可能是另一个提交。
   解析已有主线 tag 的 SHA，版本与平台输入先校验，所有质量门和构建固定同 SHA；
   清单增加 revision。拒绝覆盖已发布 Release，所选平台全部成功才发布冻结。
4. 较旧 Docker 构建后完成可覆盖 latest，手动旧源码同样可覆盖。
   固定版本/SHA 构建与 latest 提升分离，后者串行并检查是否仍为当前 main。
5. main 无分支保护，版本标签未受统一不可变规则保护。
   实际创建 main-history 与 version-tags；关闭远程 rebase merge。
   普通 PR/独立复审/质量门和自动版本写回的完整联合强制仍有平台条件，见下节。

## 实际本地验证

修复前新协作用例：14 failed、1 passed。
修复后适用版本、协作、标签及工作流回归首次 86 passed；
追加远程规则结构回归后 **87 passed，66.58 秒**。
最后补充桌面发布草稿/已发布状态的实际 Shell 回归，工作流集 **15 passed**；
此前已通过的版本、并发和标签用例没有再次改动。
同时对现有真实 0.4.4 自动版本提交执行严格验证，成功解析其来源
`a3bd9ea04a621e1de737ba1d84aa40d8d1ea8646`。

```bash
PYTHONPATH=backend:scripts <主仓库 backend/.venv/bin/python> -m pytest \
  scripts/test_collaborative_versioning.py scripts/test_advance_version.py \
  scripts/test_tag_version.py scripts/test_versioning.py \
  scripts/test_version_workflows.py backend/tests/test_version_source.py -q
```

Ruff 检查本次五个脚本通过；actionlint 检查 CI、Docker、桌面三份 workflow 通过；
git diff --check 通过。测试使用临时 bare Git 仓库，不写生产数据或历史标签。

## 远程规则实测

- 初始：main protected=false，branch protection 404 Branch not protected，
  有效 main rules=[]，repository rulesets=[]。
- 已生效：main-history（25287895），main 禁止删除和非快进，无 bypass。
- 已生效：version-tags（25287896），v* 禁止更新和删除，无 bypass，允许创建。
- main-integration 创建被 GitHub 拒绝 HTTP 422：
  `Actor GitHub Actions integration must be part of the ruleset source or owner organization`。
  当前为个人公共仓库；没有为所有开发者增加 bypass，也没有删除有效保护。
- PR/复审与 required checks 模板分开，管理员复审例外不能绕过质量规则；
  需要专用已安装 GitHub App 才能允许自动版本写回并完整强制人员通过 PR。
  模板存在不代表已经启用，完整 PR/质量规则未声称生效。

## 交付边界

源码标签、桌面 Release 和生产状态独立。未运行桌面多平台打包及真实镜像构建，
不声称这些产物已验收。旧任务与生产不受本次本地验证写入影响。
远程 CI、提交及合并结果按实际交付记录补充；本报告不提前声称已合并。
