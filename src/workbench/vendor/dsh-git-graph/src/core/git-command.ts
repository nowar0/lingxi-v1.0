// Lingxi host adapter: validation is repeated by Git on the server.
export const WORKTREE_BRANCH_PREFIX = 'lingxi/';
export function validateBranchName(name: string): string | null {
  return !name || name.startsWith('-') || /[\s~^:?*\[\\]/.test(name) || name.includes('..') || name.endsWith('.') || name.includes('@{') ? 'invalid' : null;
}
export function sanitizeWorktreeName(name: string): string | null {
  const value=name.trim(); return /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(value)?value:null;
}
