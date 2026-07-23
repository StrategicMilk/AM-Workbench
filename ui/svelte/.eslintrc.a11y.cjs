module.exports = {
  root: false,
  extends: ['plugin:svelte/recommended', 'plugin:jsx-a11y/recommended'],
  plugins: ['svelte', 'jsx-a11y'],
  rules: {
    'jsx-a11y/alt-text': 'error',
    'jsx-a11y/aria-role': 'error',
    'jsx-a11y/click-events-have-key-events': 'error',
    'jsx-a11y/interactive-supports-focus': 'error',
    'jsx-a11y/no-noninteractive-element-interactions': 'error',
    'jsx-a11y/no-static-element-interactions': 'error',
    'svelte/no-at-html-tags': 'error',
  },
};
