import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, linkify: true, breaks: true })
const defaultLinkOpen = markdown.renderer.rules.link_open || ((tokens, index, options, _env, self) => (
  self.renderToken(tokens, index, options)
))
markdown.validateLink = (url) => {
  const value = String(url ?? '').trim()
  if (!/^(https?:|mailto:)/i.test(value)) return false
  try {
    const protocol = new URL(value).protocol
    return protocol === 'http:' || protocol === 'https:' || protocol === 'mailto:'
  } catch {
    return false
  }
}
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  tokens[index].attrSet('target', '_blank')
  tokens[index].attrSet('rel', 'noopener noreferrer')
  return defaultLinkOpen(tokens, index, options, env, self)
}
markdown.renderer.rules.image = () => ''

export function renderMarkdown(content) {
  return markdown.render(String(content ?? ''))
}
