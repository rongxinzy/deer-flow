import { afterEach, beforeEach, describe, expect, it, rs } from "@rstest/core";
import {
  act,
  cleanup,
  fireEvent,
  render,
  waitFor,
} from "@testing-library/react";
import { createRef } from "react";
import { StickToBottom, type StickToBottomContext } from "use-stick-to-bottom";

import { VirtualMessageList } from "@/components/workspace/messages/virtual-message-list";
import type { MessageGroup } from "@/core/messages/utils";

const observers = new Map<Element, ResizeObserverCallback>();

beforeEach(() => {
  rs.stubGlobal(
    "ResizeObserver",
    class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(element: Element) {
        observers.set(element, this.callback);
      }
      unobserve(element: Element) {
        observers.delete(element);
      }
      disconnect() {
        for (const [element, callback] of observers) {
          if (callback === this.callback) observers.delete(element);
        }
      }
    },
  );
});
afterEach(() => {
  cleanup();
  observers.clear();
  rs.unstubAllGlobals();
});

function groups(count: number): MessageGroup[] {
  return Array.from({ length: count }, (_, index) => ({
    id: `message-${index}`,
    type: "human",
    messages: [
      { type: "human", id: `message-${index}`, content: `Question ${index}` },
    ],
  }));
}

function resize(context: StickToBottomContext, height: number) {
  const element = context.contentRef.current!;
  const callback = observers.get(element)!;
  callback(
    [
      {
        target: element,
        contentRect: new DOMRectReadOnly(0, 0, 0, height),
        borderBoxSize: [],
        contentBoxSize: [],
        devicePixelContentBoxSize: [],
      },
    ],
    {} as ResizeObserver,
  );
}

describe("VirtualMessageList scroll following", () => {
  it("resumes following when returning to the bottom during a row resize", async () => {
    const contextRef = createRef<StickToBottomContext>();
    const view = (count: number) => (
      <StickToBottom initial="instant" resize="instant" contextRef={contextRef}>
        <StickToBottom.Content>
          <VirtualMessageList
            groups={groups(count)}
            isLoading={false}
            renderGroup={(group) => group.id}
          />
        </StickToBottom.Content>
      </StickToBottom>
    );
    const { rerender } = render(view(2));
    const viewport = contextRef.current!.scrollRef.current!;
    let height = 1000;
    Object.defineProperties(viewport, {
      clientHeight: { configurable: true, get: () => 500 },
      scrollHeight: { configurable: true, get: () => height },
    });
    viewport.style.overflow = "auto";
    act(() => resize(contextRef.current!, height));
    await waitFor(() => expect(viewport.scrollTop).toBe(499));

    act(() => {
      fireEvent.wheel(viewport, { deltaY: -1000 });
      viewport.scrollTop = 0;
      fireEvent.scroll(viewport);
    });
    await waitFor(() => expect(contextRef.current!.isAtBottom).toBe(false));
    act(() => {
      height = 1100;
      resize(contextRef.current!, height);
      viewport.scrollTop = 599;
      fireEvent.scroll(viewport);
    });
    await waitFor(() =>
      expect(contextRef.current!.state.resizeDifference).toBe(0),
    );
    expect(contextRef.current!.isAtBottom).toBe(true);
    expect(contextRef.current!.state.isAtBottom).toBe(false);

    height = 1300;
    rerender(view(3));
    await waitFor(() => expect(viewport.scrollTop).toBe(799));
    expect(contextRef.current!.state.isAtBottom).toBe(true);

    // Subsequent streaming row growth must keep following without a new group.
    act(() => resize(contextRef.current!, height));
    await waitFor(() =>
      expect(contextRef.current!.state.resizeDifference).toBe(0),
    );
    act(() => {
      height = 1500;
      resize(contextRef.current!, height);
    });
    await waitFor(() => expect(viewport.scrollTop).toBe(999));

    // A fresh user scroll still escapes the restored lock. Neither another
    // submitted group nor a streaming resize may pull them out of history.
    act(() => {
      fireEvent.wheel(viewport, { deltaY: -1000 });
      viewport.scrollTop = 0;
      fireEvent.scroll(viewport);
    });
    await waitFor(() => expect(contextRef.current!.isAtBottom).toBe(false));
    height = 1700;
    rerender(view(4));
    act(() => resize(contextRef.current!, height));
    await waitFor(() =>
      expect(contextRef.current!.state.resizeDifference).toBe(0),
    );
    expect(viewport.scrollTop).toBe(0);
    expect(contextRef.current!.state.isAtBottom).toBe(false);
  });
});
