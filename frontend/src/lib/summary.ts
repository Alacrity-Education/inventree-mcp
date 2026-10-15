import type { RoleMap } from './api';
import { humanize } from './api';

/** Compact text for an effective role map, e.g. "part: view, change". */
export function summarizeRoles(map: RoleMap): {
  count: number;
  lines: string[];
} {
  const lines: string[] = [];
  for (const [name, f] of Object.entries(map).sort(([a], [b]) =>
    a.localeCompare(b)
  )) {
    const parts = (['view', 'add', 'change', 'delete'] as const).filter(
      (k) => f[k]
    );
    if (parts.length)
      lines.push(`${f.label ?? humanize(name)}: ${parts.join(', ')}`);
  }
  return { count: lines.length, lines };
}
