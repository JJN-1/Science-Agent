import { memo } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Agent 输出的 Markdown 渲染。
 * 安全性：默认不渲染内嵌 HTML（未启用 rehype-raw），模型输出中的 HTML 标签按纯文本展示。
 */
function MarkdownImpl({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

export const Markdown = memo(MarkdownImpl)
