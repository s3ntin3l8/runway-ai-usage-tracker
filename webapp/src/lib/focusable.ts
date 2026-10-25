// Whether a component renders its own focusable element.
//
// A wrapper that adds a tab stop (the tooltip's focusable host) must not wrap
// a child that is already focusable — that would make every such control two
// tab stops. Host tags can be detected from their element type; a custom
// component has to say so.

import { isValidElement } from 'react';

export interface FocusableTrigger {
  isFocusableTrigger?: boolean;
}

/** Host tags that are focusable without an explicit tabindex. */
const FOCUSABLE_TAGS = new Set(['a', 'button', 'input', 'select', 'textarea', 'summary']);

/**
 * Marks a component as rendering its own focusable element, so a wrapper
 * leaves it alone.
 *
 *     export const Button = markFocusable(function Button(props) { ... });
 */
export function markFocusable<T>(component: T): T {
  (component as FocusableTrigger).isFocusableTrigger = true;
  return component;
}

/** Whether the given child is already focusable. */
export function rendersFocusableElement(child: React.ReactNode): boolean {
  if (!isValidElement(child)) return false;
  const props = child.props as { tabIndex?: number };
  // An explicit tabindex decides it — including tabIndex={-1}, which is not
  // reachable by keyboard but is not ours to change.
  if (typeof props.tabIndex === 'number') return props.tabIndex >= 0;
  const type = child.type;
  if (typeof type === 'string') return FOCUSABLE_TAGS.has(type);
  return (type as FocusableTrigger).isFocusableTrigger === true;
}
