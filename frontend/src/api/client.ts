export class ApiError extends Error {
  status: number;
  payload: any;

  constructor(message: string, status: number, payload: any = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8124';

export const apiClient = async <T>(endpoint: string, options: RequestInit = {}): Promise<T> => {
  const url = `${BASE_URL}${endpoint}`;
  
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });

  if (!res.ok) {
    let payload = null;
    try {
      payload = await res.json();
    } catch (_) {
      // Ignore
    }

    throw new ApiError(
      payload?.detail || payload?.message || 'Request failed',
      res.status,
      payload
    );
  }

  // Handle empty responses
  if (res.status === 204) return null as unknown as T;

  const text = await res.text();
  return text ? JSON.parse(text) : null as unknown as T;
};
