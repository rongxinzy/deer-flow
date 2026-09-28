/**
 * About-content markdown for the Zhiyuan digital-employee platform. Inlined
 * to avoid raw-loader dependency (Turbopack cannot resolve raw-loader for
 * .md imports). Product copy uses the Zhiyuan name only; the upstream
 * DeerFlow project is credited once here (MIT).
 */
import { APP_VERSION } from "@/version";

export const aboutMarkdown = `# 知远数字员工 · ${APP_VERSION}

> 让每位员工都有一位随叫随到的数字同事

知远数字员工平台为企业提供可治理的智能体运行时：每位数字员工拥有独立的
会话与长期记忆，接入企业身份与权限体系，通过统一模型网关调用大模型，
并以网页对话、企业微信等渠道提供服务。

---

## 🚀 核心能力

* **对话与协作**：多轮对话、任务拆解、成果物（幻灯片 / 网页 / 报表）生成
* **长期记忆**：跨会话记住你的偏好、事实与上下文，越用越懂你
* **技能扩展**：内置与自定义技能，接入企业工具与知识
* **企业治理**：统一身份、权限与审计，模型凭证全部由平台服务端托管
* **多渠道接入**：网页对话、企业微信等渠道随时响应

---

## 📄 开源致谢

本平台基于开源项目 [DeerFlow](https://github.com/bytedance/deer-flow)
（MIT License）构建，谨致谢意。
`;
