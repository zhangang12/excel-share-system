# 同辉智能 项目管理系统(PMS)

小型非标设备制造企业的项目管理系统:销售/设计/电工/采购/仓库/生产/物流/财务/售后/OA 十部门全流程。
FastAPI(async SQLAlchemy 2.0 + Pydantic v2)+ Vue3 + TypeScript + Element Plus;开发用 SQLite,**生产是 Postgres**。

## 接手必读

1. **先读 `交接文档.md`**——工作流约定、生产踩坑、排障流程、当前状态都在里面。
2. 下一步开发依据:`盈利改善功能规划.md`。文档地图见交接文档第一节。

## 标准规则(每次都要遵守)

- **每次代码/流程变更,推送前同步更新 `交接文档.md`**(至少刷新第六节"当前状态与待办",
  新的坑/约定补进对应章节),与变更同一个 commit 提交。——用户指定的固定要求。
- 涉及 SQL/schema/GROUP BY 的改动,必须在真 Postgres 上验证(沙箱:`service postgresql start`,
  测试用户 pms/pms),SQLite 通过不算数。
- 新写/改动的后端接口必须实际调用验证,前端必须过 `npx vue-tsc --noEmit`。
- 推送:main + 当前 claude/* 开发分支双推;PAT 放 `.env.local`(gitignored)变量名 `GITHUB_PAT`,
  推送命令模式见交接文档第二节;**密钥永不落仓库**。
- 推完代码提醒用户在服务器 `git pull && docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build`
  (改前端或加依赖必须 --build)。
- commit 前设置:`git config user.email "noreply@anthropic.com" && git config user.name "Claude"`;
  不要把模型标识写进 commit/PR/代码。

## 常用命令

- 前端类型检查:`cd frontend && npx vue-tsc --noEmit`
- 后端测试:`cd backend && python -m pytest tests/ -x -q`
- 生产排障:让用户跑 `bash ops/diagnose.sh` 拿诊断包,后端日志 grep `未捕获异常`
