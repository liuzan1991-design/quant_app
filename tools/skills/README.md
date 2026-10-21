# tools/skills —— 运维 skill 的仓库副本（**唯一真源**）

## 为什么在这里

WorkBuddy 加载 skill 时只认这个路径：

```
C:\Users\<user>\.workbuddy\skills\<skill-name>\
```

该目录**不在 git 仓库内**，也不在原来的冷备 `SRC` 范围内。2026-10-21 之前，
`quant-close-inspection` 这份 823 行、69 KB 的巡检规程**在整条备份链上只有 C 盘一份**
（改前快照是手动打的，改后版本无任何副本）。

现在：**仓库是真源**，C 盘那份是**部署副本**，由 `scripts/skill_sync.py` 单向发布。

## 规则（只有一条重要的）

> **改 skill 只改 `tools/skills/<name>/`，改完必须 `--push`。**
> 不要直接改 C 盘那份 —— 那样改动只在本地、不进 git、当晚冷备虽会带上但**没人知道它变了**。

```bash
python scripts/skill_sync.py            # 只读检查：一致 exit 0 / 漂移 exit 1
python scripts/skill_sync.py --push     # 仓库 -> C 盘（发布，改完必跑）
python scripts/skill_sync.py --pull     # C 盘 -> 仓库（救回误改在部署侧的改动）
```

`--push` 前会把 C 盘目录**整包留档**到 `~/quant_app_backups/skill_push_<时间戳>.tar.gz`
（含 `.bak`），出问题可整体回滚。

## 范围

| 进仓库 | 不进仓库 |
|---|---|
| `SKILL.md` | `*.bak*`（本地留档，已被 `.gitignore` 忽略） |
| `scripts/**/*.py` | 运行时产物 |

`.bak` 存在**不算漂移** —— 同步脚本只比对受管文件。

## 覆盖现状

| 环节 | 是否覆盖 |
|---|---|
| git 历史 / 版本对比 | ✅ 本目录 |
| GitHub 异地私有副本 | ✅ 随仓库推送 |
| 每日 22:30 冷备 | ✅ `ALSO_COPY` 目录项 → `_external/skills/` |
| 磁盘级故障 | ✅（GitHub 侧） |

## 已知边界

- **`--push` 不是自动的。** 没有 hook 能在"改完立刻推送"这个时点触发；
  防线是**收市巡检里跑一次 `--check`**（skill ⑦ 节已加）。漂移会被发现，但不会自动修复。
- skill 名称含连字符，目录名必须与 `~/.workbuddy/skills/` 下的名字**逐字相同**，
  否则 WorkBuddy 不会加载（同步脚本按常量 `SKILL_NAME` 定位，改名要同步改常量）。
