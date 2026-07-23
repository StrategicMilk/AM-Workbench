export function handleEscapeKey(event: KeyboardEvent, handler: () => void): void {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  handler();
}

export function isActivationKey(event: KeyboardEvent): boolean {
  return event.key === 'Enter' || event.key === ' ';
}

export function handleActivationKey(event: KeyboardEvent, handler: () => void): void {
  if (!isActivationKey(event)) return;
  event.preventDefault();
  handler();
}
