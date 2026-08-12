import { atlasAssetToPhotoAsset } from "../query/api";
import type { AtlasAsset, PhotoAsset } from "../query/types";

export const MAX_BASKET_ITEMS = 240;

export interface BasketItem {
  id: string;
  title: string;
  subtitle: string;
  imageUrl: string;
}

export type BasketSource = AtlasAsset | PhotoAsset;

export function basketSignature(items: readonly BasketItem[]): string {
  return items.map((item) => item.id).join("\u001f");
}

export function mergeBasketItems(
  currentItems: readonly BasketItem[],
  nextItems: readonly BasketItem[],
): BasketItem[] {
  const byId = new Map(currentItems.map((item) => [item.id, item]));
  for (const item of nextItems) {
    byId.set(item.id, item);
  }
  return [...byId.values()].slice(0, MAX_BASKET_ITEMS);
}

export function basketItemFromPhotoAsset(photo: PhotoAsset): BasketItem {
  return {
    id: photo.id,
    title: photo.title,
    subtitle: [photo.location, photo.takenAt].filter(Boolean).join(" · "),
    imageUrl: photo.imageUrl,
  };
}

export function basketItemFromAtlasAsset(
  asset: AtlasAsset,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): BasketItem {
  const photo = atlasAssetToPhotoAsset(asset, index, apiBase, imageLibraryDir);
  return basketItemFromPhotoAsset(photo);
}

export function basketItemFromSource(
  source: BasketSource,
  index: number,
  apiBase: string,
  imageLibraryDir: string | null | undefined,
): BasketItem {
  return "relative_path" in source
    ? basketItemFromAtlasAsset(source, index, apiBase, imageLibraryDir)
    : basketItemFromPhotoAsset(source);
}
