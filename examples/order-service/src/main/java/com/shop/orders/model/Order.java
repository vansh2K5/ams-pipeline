package com.shop.orders.model;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.math.BigDecimal;
import java.time.LocalDateTime;
import java.util.List;
import lombok.Data;

@Data
public class Order {
    private Long orderId;
    private Long customerId;
    private BigDecimal totalAmount;
    private String currencyCode;
    private LocalDateTime createdAt;
    private OrderStatus orderStatus;
    private List<OrderItem> items;

    @JsonProperty("shipping_city")
    private String shippingCity;
}
