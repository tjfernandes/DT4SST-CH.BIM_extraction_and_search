// Ponto único de acesso HTTP ao backend HBIM.
// A chave é lida de VITE_API_KEY (inlined no bundle pelo Vite — é uma chave
// de cliente visível no browser, não um segredo de servidor) e nunca é
// registada em consola, serializada em mensagens ou incluída em URLs.

export type MessageRole = 'user' | 'assistant';

export type SearchConditionValue = string | number | Array<string | number>;

export interface SearchCondition {
  field: string;
  op: string;
  value: SearchConditionValue;
}

export interface SearchPlanPayload {
  search_strategy?: string;
  page_size?: number;
  ifc_class?: string | null;
  conditions?: SearchCondition[];
  [key: string]: unknown;
}

export interface PaginationPayload {
  stored_plan: SearchPlanPayload;
  offset: number;
  original_query: string;
}

export interface ChatRequestPayload {
  message: string;
  history: Array<{ role: MessageRole; content: string }>;
  pagination?: PaginationPayload;
  result_ids?: string[];
}

export interface ChatApiResponse {
  response: string;
  plan?: SearchPlanPayload | null;
  total_hits?: number;
  result_from?: number;
  result_count?: number;
  result_ids?: string[];
}

export type ApiErrorCode = 'unauthorized' | 'forbidden' | 'network' | 'unknown';

export class ApiError extends Error {
  readonly status: number;
  readonly code: ApiErrorCode;

  constructor(status: number, code: ApiErrorCode, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000';
const API_KEY: string | undefined = import.meta.env.VITE_API_KEY;

export async function postChat(payload: ChatRequestPayload): Promise<ChatApiResponse> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (API_KEY) {
    headers['X-API-Key'] = API_KEY;
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/chat`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
  } catch {
    throw new ApiError(0, 'network', 'Falha de rede ao contactar o servidor.');
  }

  if (response.status === 401) {
    throw new ApiError(401, 'unauthorized', 'Pedido não autenticado.');
  }
  if (response.status === 403) {
    throw new ApiError(403, 'forbidden', 'Acesso não permitido.');
  }
  if (!response.ok) {
    throw new ApiError(response.status, 'unknown', 'Falha na comunicação com o servidor.');
  }
  return (await response.json()) as ChatApiResponse;
}
