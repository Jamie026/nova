/**
 * Icono de la bandeja del sistema, incrustado como data URL.
 *
 * Es un recorte de la cabeza del retrato que usa el avatar: a 32 px la figura
 * completa no se distingue, la cara sí. Va embebido y no como archivo suelto
 * para que el empaquetado no dependa de copiar assets a la carpeta correcta.
 * Se regenera con scripts/make-tray-icon.py si cambias el retrato.
 */
export const TRAY_ICON_DATA_URL =
  'data:image/png;base64,' +
  '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';
