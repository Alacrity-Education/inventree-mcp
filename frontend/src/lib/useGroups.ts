import { type Api, type Group, listGroups } from './api';
import { type AsyncState, useAsync } from './useAsync';

/** Core's group list (`GET /api/user/group/`), sorted by name. */
export function useGroups(api: Api): AsyncState<Group[]> {
  return useAsync(() => listGroups(api), [api]);
}
