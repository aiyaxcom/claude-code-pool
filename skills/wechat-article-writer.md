---
name: wechat-article-writer
description: 微信公众号文章创作 skill，用于创作高质量的文章内容。Invoke with article title as argument: /wechat-article-writer <文章标题>
---

# 微信公众号文章创作

用于根据用户需求创作微信公众号文章。

**当前文章标题：$ARGUMENTS**

## 使用场景

当用户需要创作微信公众号文章、撰写内容、生成 Markdown 格式文本时触发此 skill。

## 文章风格要求

- 标题醒目，能吸引读者
- 段落分明，每段 2-4 句话
- 适当使用列表、引用等格式
- 结尾有总结或引导互动
- 保持客观、专业的语气

## 图片插入机制

文章中使用特殊标记，后续处理时替换为实际图片：

- `[IMAGE:url_or_description]` - 普通图片
- `[SCREENSHOT:url]` - 网页截图

示例：
```
[SCREENSHOT:https://tools.aiyax.com]
[IMAGE:一张展示 AI 工具界面的截图]
```

## 输出格式

直接输出 Markdown 格式的文章内容：
1. 文章标题（二级标题）
2. 正文内容（带适当格式）
3. 图片标记（如需要）

## 注意事项

- 不输出代码块包裹的 Markdown，直接输出内容
- 文章长度适中，一般 800-2000 字
- 不涉及敏感内容、违法违规信息