import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchAtlasBasket, saveAtlasBasket } from "../query/api";
import type { AtlasAsset } from "../query/types";
import {
  basketItemFromAtlasAsset,
  basketItemFromSource,
  basketSignature,
  mergeBasketItems,
  type BasketItem,
  type BasketSource,
} from "./model";

export type BasketPersistencePhase = "waiting" | "idle" | "saving" | "saved" | "error";

export interface BasketPersistenceState {
  phase: BasketPersistencePhase;
  error: string | null;
  isHydrated: boolean;
}

interface UsePersistedAtlasBasketOptions {
  apiBase: string;
  scope: string | null;
  selectedImageLibraryDir?: string | null;
  fallbackImageLibraryDir?: string | null;
  connectionState: "checking" | "connected" | "mock" | "offline";
}

interface UsePersistedAtlasBasketResult {
  items: BasketItem[];
  assetIds: string[];
  toggle: (asset: AtlasAsset) => void;
  addMany: (sources: readonly BasketSource[]) => void;
  remove: (assetId: string) => void;
  clear: () => void;
  retry: () => void;
  persistence: BasketPersistenceState;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function usePersistedAtlasBasket({
  apiBase,
  scope,
  selectedImageLibraryDir,
  fallbackImageLibraryDir,
  connectionState,
}: UsePersistedAtlasBasketOptions): UsePersistedAtlasBasketResult {
  const [items, setItems] = useState<BasketItem[]>([]);
  const [isHydrated, setIsHydrated] = useState(false);
  const [phase, setPhase] = useState<BasketPersistencePhase>("waiting");
  const [error, setError] = useState<string | null>(null);
  const [loadRetryKey, setLoadRetryKey] = useState(0);
  const [saveRetryKey, setSaveRetryKey] = useState(0);
  const scopeRef = useRef<string | null>(null);
  const lastPersistedSignatureRef = useRef("");
  const desiredSignatureRef = useRef("");
  const desiredAssetIdsRef = useRef<string[]>([]);
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const saveAbortControllerRef = useRef<AbortController | null>(null);
  const assetIds = useMemo(() => items.map((item) => item.id), [items]);
  const imageLibraryDir = selectedImageLibraryDir ?? fallbackImageLibraryDir ?? null;

  useEffect(() => {
    if (scopeRef.current === scope) {
      return;
    }
    scopeRef.current = scope;
    lastPersistedSignatureRef.current = "";
    desiredSignatureRef.current = "";
    desiredAssetIdsRef.current = [];
    saveAbortControllerRef.current?.abort();
    saveQueueRef.current = Promise.resolve();
    setItems([]);
    setIsHydrated(false);
    setPhase("waiting");
    setError(null);
  }, [scope]);

  useEffect(() => {
    if (connectionState !== "connected" || !scope) {
      return;
    }

    const controller = new AbortController();
    let disposed = false;
    const timeoutId = window.setTimeout(() => controller.abort(), 5000);
    const capturedScope = scope;
    setPhase("waiting");
    setError(null);

    void fetchAtlasBasket({
      apiBase,
      dbPath: capturedScope,
      signal: controller.signal,
    })
      .then((basket) => {
        if (disposed || scopeRef.current !== capturedScope) {
          return;
        }
        const restoredItems = basket.assets.map((asset, index) =>
          basketItemFromAtlasAsset(asset, index, apiBase, imageLibraryDir),
        );
        lastPersistedSignatureRef.current = basketSignature(restoredItems);
        setItems((currentItems) => mergeBasketItems(restoredItems, currentItems));
        setIsHydrated(true);
        setPhase("idle");
      })
      .catch((loadError: unknown) => {
        if (disposed || scopeRef.current !== capturedScope) {
          return;
        }
        setPhase("error");
        setError(
          isAbortError(loadError)
            ? "Selection sync timed out. Retry before editing this library selection."
            : `Selection sync is paused: ${loadError instanceof Error ? loadError.message : "basket could not be loaded"}`,
        );
      })
      .finally(() => window.clearTimeout(timeoutId));

    return () => {
      disposed = true;
      window.clearTimeout(timeoutId);
      controller.abort();
    };
  }, [
    apiBase,
    connectionState,
    fallbackImageLibraryDir,
    loadRetryKey,
    scope,
    selectedImageLibraryDir,
  ]);

  useEffect(() => {
    if (!isHydrated || connectionState !== "connected") {
      return;
    }

    const signature = assetIds.join("\u001f");
    desiredSignatureRef.current = signature;
    desiredAssetIdsRef.current = [...assetIds];
    if (signature === lastPersistedSignatureRef.current) {
      setPhase("idle");
      setError(null);
      return;
    }

    setPhase("saving");
    setError(null);

    const timer = window.setTimeout(() => {
      saveQueueRef.current = saveQueueRef.current
        .catch(() => undefined)
        .then(async () => {
          const capturedScope = scope;
          while (
            scopeRef.current === capturedScope
            && desiredSignatureRef.current !== lastPersistedSignatureRef.current
          ) {
            const queuedSignature = desiredSignatureRef.current;
            const queuedAssetIds = [...desiredAssetIdsRef.current];
            const controller = new AbortController();
            saveAbortControllerRef.current = controller;
            const timeoutId = window.setTimeout(() => controller.abort(), 10_000);
            try {
              await saveAtlasBasket({
                apiBase,
                dbPath: capturedScope,
                assetIds: queuedAssetIds,
                name: "Current selection",
                signal: controller.signal,
              });
              if (scopeRef.current !== capturedScope) {
                return;
              }
              lastPersistedSignatureRef.current = queuedSignature;
              if (desiredSignatureRef.current === queuedSignature) {
                setPhase("saved");
                setError(null);
              }
            } catch (saveError) {
              if (scopeRef.current !== capturedScope) {
                return;
              }
              setPhase("error");
              setError(
                isAbortError(saveError)
                  ? "Saving the selection timed out. Retry to save the latest selection."
                  : saveError instanceof Error
                    ? saveError.message
                    : "The current selection could not be saved.",
              );
              return;
            } finally {
              window.clearTimeout(timeoutId);
              if (saveAbortControllerRef.current === controller) {
                saveAbortControllerRef.current = null;
              }
            }
          }
        });
    }, 400);

    return () => {
      window.clearTimeout(timer);
    };
  }, [apiBase, assetIds, connectionState, isHydrated, saveRetryKey, scope]);

  useEffect(
    () => () => {
      saveAbortControllerRef.current?.abort();
    },
    [],
  );

  const toggle = useCallback((asset: AtlasAsset): void => {
    setItems((currentItems) => {
      if (currentItems.some((item) => item.id === asset.id)) {
        return currentItems.filter((item) => item.id !== asset.id);
      }
      const nextItem = basketItemFromAtlasAsset(
        asset,
        currentItems.length,
        apiBase,
        imageLibraryDir,
      );
      return mergeBasketItems(currentItems, [nextItem]);
    });
  }, [apiBase, imageLibraryDir]);

  const addMany = useCallback((sources: readonly BasketSource[]): void => {
    if (sources.length === 0) {
      return;
    }
    setItems((currentItems) =>
      mergeBasketItems(
        currentItems,
        sources.map((source, index) =>
          basketItemFromSource(
            source,
            currentItems.length + index,
            apiBase,
            imageLibraryDir,
          ),
        ),
      ),
    );
  }, [apiBase, imageLibraryDir]);

  const remove = useCallback((assetId: string): void => {
    setItems((currentItems) => currentItems.filter((item) => item.id !== assetId));
  }, []);

  const clear = useCallback((): void => {
    setItems([]);
  }, []);

  const retry = useCallback((): void => {
    if (isHydrated) {
      setSaveRetryKey((current) => current + 1);
    } else {
      setLoadRetryKey((current) => current + 1);
    }
  }, [isHydrated]);

  const persistence = useMemo<BasketPersistenceState>(() => ({
    phase,
    error,
    isHydrated,
  }), [error, isHydrated, phase]);

  return {
    items,
    assetIds,
    toggle,
    addMany,
    remove,
    clear,
    retry,
    persistence,
  };
}
