package com.shop.orders.model;

import java.math.BigDecimal;
import lombok.Data;

@Data
public class OrderItem {
    private String sku;
    private Integer quantity;
    private BigDecimal unitPrice;
}
