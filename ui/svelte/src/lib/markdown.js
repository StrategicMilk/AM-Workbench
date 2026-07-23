import { marked } from 'marked';
import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import css from 'highlight.js/lib/languages/css';
import javascript from 'highlight.js/lib/languages/javascript';
import json from 'highlight.js/lib/languages/json';
import markdownLanguage from 'highlight.js/lib/languages/markdown';
import plaintext from 'highlight.js/lib/languages/plaintext';
import powershell from 'highlight.js/lib/languages/powershell';
import python from 'highlight.js/lib/languages/python';
import shell from 'highlight.js/lib/languages/shell';
import typescript from 'highlight.js/lib/languages/typescript';
import xml from 'highlight.js/lib/languages/xml';
import yaml from 'highlight.js/lib/languages/yaml';

const SAFE_PROTOCOLS = new Set(['http:', 'https:', 'mailto:']);

const HIGHLIGHT_LANGUAGES = {
  bash,
  css,
  javascript,
  json,
  markdown: markdownLanguage,
  plaintext,
  powershell,
  python,
  shell,
  typescript,
  xml,
  yaml,
};

for (const [name, language] of Object.entries(HIGHLIGHT_LANGUAGES)) {
  hljs.registerLanguage(name, language);
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function safeUrl(value) {
  const raw = String(value ?? '').trim();
  if (!raw) {
    return '#';
  }
  try {
    const parsed = new URL(raw, 'https://vetinari.local');
    if (parsed.origin === 'https://vetinari.local' && raw.startsWith('/')) {
      return escapeAttribute(raw);
    }
    if (SAFE_PROTOCOLS.has(parsed.protocol)) {
      return escapeAttribute(raw);
    }
  } catch {
    return '#';
  }
  return '#';
}

function safeRenderedText(value) {
  return typeof value === 'string' ? value : escapeHtml(value ?? '');
}

function buildRenderer() {
  const renderer = new marked.Renderer();

  renderer.html = (html) => escapeHtml(html);

  renderer.link = (href, title, text) => {
    const safeHref = safeUrl(href);
    const safeTitle = title ? ` title="${escapeAttribute(title)}"` : '';
    const externalAttrs = safeHref === '#' ? ' aria-disabled="true"' : ' target="_blank" rel="noopener noreferrer"';
    return `<a href="${safeHref}"${safeTitle}${externalAttrs}>${safeRenderedText(text)}</a>`;
  };

  renderer.image = (href, title, text) => {
    const safeSrc = safeUrl(href);
    const safeAlt = escapeAttribute(text ?? '');
    const safeTitle = title ? ` title="${escapeAttribute(title)}"` : '';
    return `<img src="${safeSrc}" alt="${safeAlt}"${safeTitle}>`;
  };

  return renderer;
}

export function renderSafeMarkdown(markdown) {
  return marked.parse(escapeHtml(markdown ?? ''), {
    breaks: true,
    gfm: true,
    renderer: buildRenderer(),
    highlight(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value;
      }
      return hljs.highlightAuto(code).value;
    },
  });
}
