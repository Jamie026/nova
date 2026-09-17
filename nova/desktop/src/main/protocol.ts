/**
 * Espejo TypeScript del contrato definido en core/nova/events.py.
 * Si cambia allí, cambia aquí: son las dos caras del mismo protocolo.
 */

export type AgentState =
  | 'IDLE' | 'WAKE_WORD_DETECTED' | 'LISTENING' | 'THINKING'
  | 'EXECUTING' | 'SPEAKING' | 'ERROR' | 'SLEEPING';

export interface CoreEvent {
  type: string;
  state?: AgentState;
  text?: string;
  partial?: boolean;
  name?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  summary?: string;
  id?: string;
  risk?: string;
  approved?: boolean;
  value?: number;
  data?: Record<string, unknown>;
}

export type UiCommand =
  | { type: 'text_input'; text: string }
  | { type: 'confirm_response'; id: string; approved: boolean }
  | { type: 'stop' }
  | { type: 'wake' }
  | { type: 'get_settings' }
  | { type: 'set_settings'; patch: Record<string, unknown> };
