# 项目文件管理系统 v2

> ⚠️ 本 README 内容偏旧（v2 时代）。**最新项目状态请看：`AGENTS.md`（项目记忆）和 `docs/项目交接文档.md`（交接文档）。**

类飞书多维表格的内部项目进度管理系统。

**技术栈**：FastAPI + PostgreSQL + Redis + Vue 3 + TypeScript + Vite + Element Plus + vxe-table

## 快速开始（开发环境）

```bash
# 1. 复制环境变量
cp .env.example .env

# 2. 启动全部服务（首次会拉镜像 + 装依赖，约 3-5 分钟）
docker compose up -d --build

# 3. 看日志（可选）
docker compose logs -f
```

启动后访问：

- 前端：http://localhost:5173
- 后端 API：http://localhost:8000/api/health
- 后端 OpenAPI 文档：http://localhost:8000/docs
- PostgreSQL：localhost:5432（pms / pms_dev_pwd）
- Redis：localhost:6379

## 常用命令

```bash
# 起服务
docker compose up -d

# 停服务
docker compose down

# 重建某个服务
docker compose up -d --build backend

# 进容器看
docker compose exec backend bash
docker compose exec postgres psql -U pms

# 看日志
docker compose logs -f backend
docker compose logs -f frontend

# 清理（包括数据卷，谨慎用）
docker compose down -v
```

## 目录结构

```
v2/
├── docker-compose.yml      # 一键启动
├── .env.example            # 环境变量模板
├── backend/
│   ├── Dockerfile.dev
│   ├── requirements.txt
│   └── app/
│       ├── main.py         # FastAPI 入口
│       ├── config.py       # 配置
│       └── database.py     # async SQLAlchemy
└── frontend/
    ├── Dockerfile.dev
    ├── package.json
    ├── vite.config.ts
    └── src/                # Vue 3 + TS
```

## Sprint 进度

- [x] Sprint 0：脚手架
- [ ] Sprint 1：认证 + 用户管理
- [ ] Sprint 2：项目与成员
- [ ] Sprint 3：数据表 + 字段 + 网格编辑
- [ ] Sprint 4：Excel 导入导出
- [ ] Sprint 5：字段级权限
- [ ] Sprint 6：实时多人协作
- [ ] Sprint 7：筛选/排序/搜索
- [ ] Sprint 8：操作审计 + 部署文档
