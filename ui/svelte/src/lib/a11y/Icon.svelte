<script>
  let { name = '', family = 'fas', class: className = '', ...rest } = $props();

  function iconToken(value) {
    const token = typeof value === 'string' ? value.trim() : '';
    if (!token) return '';
    return token.startsWith('fa-') ? token : `fa-${token}`;
  }

  let iconClass = $derived([family, iconToken(name), className].filter(Boolean).join(' '));
</script>

<i {...rest} class={iconClass} aria-hidden="true"></i>
