import React, { useEffect, useState } from 'react';

export function productImageUrls(productId) {
  return {
    photo: `/img/${productId}.jpg`,
    fallback: `/img/${productId}.svg`,
  };
}

export default function ProductImage({ productId, alt, ...props }) {
  const { photo, fallback } = productImageUrls(productId);
  const [src, setSrc] = useState(photo);

  useEffect(() => {
    setSrc(photo);
  }, [photo]);

  return <img {...props} src={src} alt={alt} onError={() => setSrc((current) => current === photo ? fallback : current)} />;
}
