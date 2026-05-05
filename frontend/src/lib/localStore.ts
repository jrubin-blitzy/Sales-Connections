import type {
  ConnectionCreate,
  ConnectionDuplicateCheckResponse,
  ConnectionHistoryEntry,
  ConnectionRead,
  ConnectionStatusUpdate,
  ConnectionUpdate,
  PaginatedConnections,
  TagCreate,
  TagRead,
} from "@/schemas/connection";

interface LocalPaginatedHistory {
  items: ConnectionHistoryEntry[];
  total: number;
  page: number;
  page_size: number;
}

const CONNECTIONS_KEY = "sc_connections";
const TAGS_KEY = "sc_tags";

function readConnections(): ConnectionRead[] {
  try {
    const raw = localStorage.getItem(CONNECTIONS_KEY);
    if (!raw) return [];
    return JSON.parse(raw) as ConnectionRead[];
  } catch {
    return [];
  }
}

function writeConnections(connections: ConnectionRead[]): void {
  localStorage.setItem(CONNECTIONS_KEY, JSON.stringify(connections));
}

function readTags(): TagRead[] {
  try {
    const raw = localStorage.getItem(TAGS_KEY);
    if (!raw) return [];
    return JSON.parse(raw) as TagRead[];
  } catch {
    return [];
  }
}

function writeTags(tags: TagRead[]): void {
  localStorage.setItem(TAGS_KEY, JSON.stringify(tags));
}

function getCurrentUser(): { id: string; display_name: string } {
  const email = localStorage.getItem("sc_user_email") ?? "unknown@blitzy.com";
  const id = localStorage.getItem("sc_user_id") ?? crypto.randomUUID();
  const namePart = email.split("@")[0] ?? "";
  const display_name = namePart
    .replace(/[._-]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return { id, display_name };
}

function normalizeLinkedInUrl(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/+$/, "");
    return `https://www.linkedin.com${path}`.toLowerCase();
  } catch {
    return url.toLowerCase();
  }
}

export interface ListConnectionsParams {
  company?: string;
  full_name_search?: string;
  involvement?: ReadonlyArray<string>;
  outreach_status?: ReadonlyArray<string>;
  owner_user_ids?: ReadonlyArray<string>;
  tag_ids?: ReadonlyArray<string>;
  submission_date_from?: string;
  submission_date_to?: string;
  include_deleted?: boolean;
  page?: number;
  page_size?: number;
  sort?: string;
  sort_dir?: string;
}

export function listConnections(params: ListConnectionsParams = {}): Promise<PaginatedConnections> {
  const all = readConnections();

  let filtered = all.filter((c) => {
    if (!params.include_deleted && c.deleted_at !== null) return false;
    if (params.company && !c.company.toLowerCase().includes(params.company.toLowerCase()))
      return false;
    if (
      params.full_name_search &&
      !c.full_name.toLowerCase().includes(params.full_name_search.toLowerCase())
    )
      return false;
    if (params.involvement?.length && !params.involvement.includes(c.involvement)) return false;
    if (params.outreach_status?.length && !params.outreach_status.includes(c.outreach_status))
      return false;
    if (params.owner_user_ids?.length && !params.owner_user_ids.includes(c.owner_user_id))
      return false;
    if (params.tag_ids?.length) {
      const cTagIds = c.tags.map((t) => t.id);
      if (!params.tag_ids.some((id) => cTagIds.includes(id))) return false;
    }
    if (params.submission_date_from && c.submission_date < params.submission_date_from)
      return false;
    if (
      params.submission_date_to &&
      c.submission_date > params.submission_date_to + "T23:59:59Z"
    )
      return false;
    return true;
  });

  const sort = params.sort ?? "submission_date";
  const sortDir = params.sort_dir ?? "desc";
  filtered = [...filtered].sort((a, b) => {
    let aVal: string;
    let bVal: string;
    switch (sort) {
      case "full_name":
        aVal = a.full_name;
        bVal = b.full_name;
        break;
      case "company":
        aVal = a.company;
        bVal = b.company;
        break;
      case "owner_display_name":
        aVal = a.owner_display_name;
        bVal = b.owner_display_name;
        break;
      case "outreach_status":
        aVal = a.outreach_status;
        bVal = b.outreach_status;
        break;
      default:
        aVal = a.submission_date;
        bVal = b.submission_date;
        break;
    }
    const cmp = aVal.localeCompare(bVal);
    return sortDir === "asc" ? cmp : -cmp;
  });

  const total = filtered.length;
  const pageSize = params.page_size ?? 25;
  const page = params.page ?? 1;
  const offset = (page - 1) * pageSize;
  const limit = pageSize;
  const items = filtered.slice(offset, offset + limit);

  return Promise.resolve({ items, total, limit, offset });
}

export function getConnection(id: string): Promise<ConnectionRead> {
  const conn = readConnections().find((c) => c.id === id);
  if (!conn) return Promise.reject(new Error(`Connection ${id} not found`));
  return Promise.resolve(conn);
}

export function createConnection(payload: ConnectionCreate): Promise<ConnectionRead> {
  const user = getCurrentUser();
  const now = new Date().toISOString();
  const allTags = readTags();
  const tags = allTags.filter((t) => payload.tag_ids.includes(t.id));

  const record: ConnectionRead = {
    id: crypto.randomUUID(),
    full_name: payload.full_name,
    linkedin_url: payload.linkedin_url,
    normalized_linkedin_url: normalizeLinkedInUrl(payload.linkedin_url),
    company: payload.company,
    job_title: payload.job_title,
    relationship_context: payload.relationship_context,
    ai_notes: payload.ai_notes ?? null,
    involvement: payload.involvement,
    outreach_status: "Not Started",
    submission_date: now,
    owner_user_id: user.id,
    owner_display_name: user.display_name,
    tags,
    created_at: now,
    updated_at: now,
    deleted_at: null,
  };

  writeConnections([record, ...readConnections()]);
  return Promise.resolve(record);
}

export function updateConnection(id: string, payload: ConnectionUpdate): Promise<ConnectionRead> {
  const all = readConnections();
  const idx = all.findIndex((c) => c.id === id);
  if (idx === -1) return Promise.reject(new Error(`Connection ${id} not found`));

  const existing = all[idx] as ConnectionRead;
  const now = new Date().toISOString();

  const tags =
    payload.tag_ids !== undefined
      ? readTags().filter((t) => payload.tag_ids!.includes(t.id))
      : existing.tags;

  const updated: ConnectionRead = {
    ...existing,
    ...(payload.full_name !== undefined && { full_name: payload.full_name }),
    ...(payload.linkedin_url !== undefined && {
      linkedin_url: payload.linkedin_url,
      normalized_linkedin_url: normalizeLinkedInUrl(payload.linkedin_url),
    }),
    ...(payload.company !== undefined && { company: payload.company }),
    ...(payload.job_title !== undefined && { job_title: payload.job_title }),
    ...(payload.relationship_context !== undefined && {
      relationship_context: payload.relationship_context,
    }),
    ...(payload.ai_notes !== undefined && { ai_notes: payload.ai_notes }),
    ...(payload.involvement !== undefined && { involvement: payload.involvement }),
    tags,
    updated_at: now,
  };

  all[idx] = updated;
  writeConnections(all);
  return Promise.resolve(updated);
}

export function updateConnectionStatus(
  id: string,
  payload: ConnectionStatusUpdate,
): Promise<ConnectionRead> {
  const all = readConnections();
  const idx = all.findIndex((c) => c.id === id);
  if (idx === -1) return Promise.reject(new Error(`Connection ${id} not found`));

  const now = new Date().toISOString();
  const updated: ConnectionRead = {
    ...(all[idx] as ConnectionRead),
    outreach_status: payload.outreach_status,
    updated_at: now,
  };
  all[idx] = updated;
  writeConnections(all);
  return Promise.resolve(updated);
}

export function softDeleteConnection(id: string): Promise<ConnectionRead> {
  const all = readConnections();
  const idx = all.findIndex((c) => c.id === id);
  if (idx === -1) return Promise.reject(new Error(`Connection ${id} not found`));

  const now = new Date().toISOString();
  const updated: ConnectionRead = {
    ...(all[idx] as ConnectionRead),
    deleted_at: now,
    updated_at: now,
  };
  all[idx] = updated;
  writeConnections(all);
  return Promise.resolve(updated);
}

export function duplicateCheck(
  linkedinUrl: string,
  excludeId?: string,
): Promise<ConnectionDuplicateCheckResponse> {
  const normalized = normalizeLinkedInUrl(linkedinUrl);
  const duplicate = readConnections().find(
    (c) =>
      c.normalized_linkedin_url === normalized &&
      c.id !== excludeId &&
      c.deleted_at === null,
  );

  return Promise.resolve(
    duplicate
      ? {
          duplicate_found: true,
          existing_record_id: duplicate.id,
          existing_owner_display_name: duplicate.owner_display_name,
          existing_submission_date: duplicate.submission_date,
          normalized_linkedin_url: normalized,
        }
      : {
          duplicate_found: false,
          existing_record_id: null,
          existing_owner_display_name: null,
          existing_submission_date: null,
          normalized_linkedin_url: normalized,
        },
  );
}

export function getConnectionHistory(id: string): Promise<LocalPaginatedHistory> {
  // No audit trail in local mode
  void id;
  return Promise.resolve({ items: [], total: 0, page: 1, page_size: 25 });
}

export function listTags(): Promise<TagRead[]> {
  const tags = readTags();
  return Promise.resolve([...tags].sort((a, b) => a.name.localeCompare(b.name)));
}

export function createTag(payload: TagCreate): Promise<TagRead> {
  const tags = readTags();
  const existing = tags.find((t) => t.name.toLowerCase() === payload.name.toLowerCase());
  if (existing) return Promise.resolve(existing);

  const now = new Date().toISOString();
  const tag: TagRead = { id: crypto.randomUUID(), name: payload.name, created_at: now };
  writeTags([...tags, tag]);
  return Promise.resolve(tag);
}
